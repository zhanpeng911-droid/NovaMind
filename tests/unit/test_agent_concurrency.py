"""Agent 按 thread 并发、取消回滚与锁注册表测试（加固 Phase 3）。

覆盖方案验证清单：
- 不同 thread 确实重叠执行；
- 同 thread run/astream 串行，不交叉修改状态；
- 同 thread 的异步删除等待在飞轮次结束后再删；其他 thread 删除不等待；
- 取消导致半轮 user/assistant 状态不残留（内存回滚 + 不落盘）；
- 锁注册表在运行结束后释放条目；
- 运行中的 thread 状态不被 LRU 淘汰（active pin）。
"""
import asyncio
import contextlib
import os
import tempfile
import unittest

from langchain_core.messages import AIMessage

from novamind.core.state_machine import AgentState, ConversationStore, NovaMindAgent


def _mkmsg(text):
    return AIMessage(content=text, id=f"msg_{text}")


class EventTrace:
    """记录 enter/exit 顺序的慢节点。"""

    def __init__(self, trace: list, delay: float = 0.08):
        self.trace = trace
        self.delay = delay

    async def __call__(self, state: AgentState) -> dict:
        tid = state.metadata["thread_id"]
        self.trace.append(("enter", tid))
        await asyncio.sleep(self.delay)
        self.trace.append(("exit", tid))
        return {"messages": [_mkmsg(f"reply-{tid}")]}


def _make_agent(trace, node=None, delay: float = 0.08, **kwargs):
    agent = NovaMindAgent(**kwargs)
    agent.add_node("agent", node or EventTrace(trace, delay=delay))
    agent.add_conditional_edge("agent", lambda s: "__end__", {"__end__": "__end__"})
    return agent


class TestThreadConcurrency(unittest.TestCase):
    def test_different_threads_overlap(self):
        trace: list = []
        agent = _make_agent(trace)

        async def _run():
            await asyncio.gather(
                agent.run("hi", thread_id="t_a"),
                agent.run("hi", thread_id="t_b"),
            )

        asyncio.run(_run())
        self.assertEqual(len(trace), 4)
        # 重叠：第一个 exit 之前，两个线程都已 enter
        first_exit_idx = next(i for i, ev in enumerate(trace) if ev[0] == "exit")
        enters_before_first_exit = sum(
            1 for ev in trace[:first_exit_idx] if ev[0] == "enter"
        )
        self.assertGreaterEqual(enters_before_first_exit, 2, trace)

    def test_same_thread_is_serialized(self):
        trace: list = []
        agent = _make_agent(trace)

        async def _run():
            await asyncio.gather(
                agent.run("hi", thread_id="t_same"),
                agent.run("hi", thread_id="t_same"),
            )

        asyncio.run(_run())
        # 串行：两次 enter 之间必然有一次 exit
        enters = [i for i, ev in enumerate(trace) if ev[0] == "enter"]
        self.assertEqual(len(enters), 2)
        first_enter, second_enter = enters
        self.assertTrue(
            any(ev[0] == "exit" for ev in trace[first_enter + 1:second_enter]),
            trace,
        )

    def test_astream_same_thread_serialized(self):
        trace: list = []
        agent = _make_agent(trace)

        async def _consume(tid):
            async for _ in agent.astream("hi", thread_id=tid):
                pass

        async def _run():
            await asyncio.gather(
                _consume("t_stream"), _consume("t_stream"),
            )

        asyncio.run(_run())
        enters = [i for i, ev in enumerate(trace) if ev[0] == "enter"]
        self.assertEqual(len(enters), 2)
        first_enter, second_enter = enters
        self.assertTrue(
            any(ev[0] == "exit" for ev in trace[first_enter + 1:second_enter]),
            trace,
        )


class TestClearConversationCoordination(unittest.TestCase):
    def test_delete_same_thread_waits_then_removes(self):
        trace: list = []
        agent = _make_agent(trace, delay=0.15)
        deleted = {}

        async def _run():
            run_task = asyncio.create_task(agent.run("hi", thread_id="t_del"))
            await asyncio.sleep(0.03)  # 让 run 先进入慢节点
            self.assertIn("t_del", agent._states)
            clear_task = asyncio.create_task(agent.aclear_conversation("t_del"))
            await asyncio.sleep(0.05)
            # run 仍在飞：删除必须还在等待
            deleted["waited"] = not clear_task.done()
            await run_task
            await clear_task
            deleted["removed"] = "t_del" not in agent._states

        asyncio.run(_run())
        self.assertTrue(deleted["waited"])
        self.assertTrue(deleted["removed"])

    def test_delete_other_thread_does_not_wait(self):
        trace: list = []
        agent = _make_agent(trace, delay=0.2)
        agent._states["t_other"] = AgentState()

        async def _run():
            run_task = asyncio.create_task(agent.run("hi", thread_id="t_busy"))
            await asyncio.sleep(0.03)
            start = asyncio.get_running_loop().time()
            await agent.aclear_conversation("t_other")
            elapsed = asyncio.get_running_loop().time() - start
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
            return elapsed

        elapsed = asyncio.run(_run())
        self.assertLess(elapsed, 0.1, "删除其他 thread 不应等待在飞轮次")

    def test_sync_clear_raises_when_busy(self):
        trace: list = []
        agent = _make_agent(trace, delay=0.2)

        async def _run():
            run_task = asyncio.create_task(agent.run("hi", thread_id="t_busy2"))
            await asyncio.sleep(0.03)
            with self.assertRaises(RuntimeError):
                agent.clear_conversation("t_busy2")
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

        asyncio.run(_run())

    def test_lock_registry_releases_entries(self):
        trace: list = []
        agent = _make_agent(trace)

        async def _run():
            await agent.run("hi", thread_id="t_reg")
            await agent.run("hi", thread_id="t_reg")
            self.assertNotIn("t_reg", agent._run_coordinator._locks)

        asyncio.run(_run())
        self.assertNotIn("t_reg", agent._run_coordinator._locks)


class TestCancelRollback(unittest.TestCase):
    def test_cancel_rolls_back_turn(self):
        tmp = tempfile.mkdtemp(prefix="novamind_p3_")
        db = os.path.join(tmp, "state.sqlite3")
        store = ConversationStore(db_path=db)

        async def slow_and_partially_updates(state: AgentState) -> dict:
            # 半轮：写入用户可见的部分更新后再挂起（模拟取消发生在中途）
            state.summary = "半轮写坏的摘要"
            await asyncio.sleep(5)
            return {"messages": [_mkmsg("never")]}

        agent = NovaMindAgent(conversation_store=store)
        agent.add_node("agent", slow_and_partially_updates)
        agent.add_conditional_edge("agent", lambda s: "__end__", {"__end__": "__end__"})

        # 先造一轮已完成的旧对话并落盘
        async def _seed():
            agent._get_or_create_state("t_cancel").add_message(_mkmsg("old"))
            agent._persist_state("t_cancel", agent._states["t_cancel"])

        asyncio.run(_seed())
        seeded_count = agent._persisted_counts["t_cancel"]

        async def _cancel():
            task = asyncio.create_task(agent.run("新输入", thread_id="t_cancel"))
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_cancel())

        state = agent._states["t_cancel"]
        # 半轮的 user 输入与写坏的摘要不残留
        self.assertEqual([m.content for m in state.messages], ["old"])
        self.assertEqual(state.summary, "")
        self.assertEqual(agent._persisted_counts["t_cancel"], seeded_count)
        # 数据库中没有本轮新消息
        rows = store.load_messages("t_cancel")
        self.assertEqual([m.content for m in rows], ["old"])
        store.close()

    def test_running_state_not_evicted_by_other_threads(self):
        trace: list = []
        agent = _make_agent(trace, max_cached_states=1)

        async def _run():
            slow = asyncio.create_task(agent.run("hi", thread_id="t_pin_a"))
            await asyncio.sleep(0.02)  # t_pin_a 已在飞且状态已建
            # 另一个 thread 的 run 触发 LRU 淘汰；max=1 下不得淘汰运行中的 a
            await agent.run("hi", thread_id="t_pin_b")
            self.assertIn("t_pin_a", agent._states)
            await slow

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
