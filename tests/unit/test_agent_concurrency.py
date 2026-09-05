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

from langchain_core.messages import AIMessage, HumanMessage

from novamind.core.middlewares import MiddlewareManager
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




class TestFinalizationOnceAndRollback(unittest.TestCase):
    """加固收尾回归：after 钩子/持久化阶段失败时——

    - after_agent 恰好尝试一次（不再被 finally 重复分发）；
    - 本轮状态（含嵌套 metadata）回滚到轮次开始前。"""

    def _make_agent_with(self, middlewares, node_fn=None, **kwargs):
        agent = NovaMindAgent(
            middleware_manager=MiddlewareManager(middlewares), **kwargs
        )

        async def default_node(state):
            return {"messages": [_mkmsg("reply")]}

        agent.add_node("agent", node_fn or default_node)
        agent.add_conditional_edge("agent", lambda s: "__end__", {"__end__": "__end__"})
        return agent

    def test_after_hook_failure_dispatches_once_and_rolls_back(self):
        """对应审查复现：after_calls 必须为 1，失败后消息回到轮次前。"""
        import asyncio as _aio

        calls = {"after": 0}

        class ExplodingAfter:
            async def aafter_agent(self, ctx):
                calls["after"] += 1
                raise RuntimeError("after hook exploded")

        agent = self._make_agent_with([ExplodingAfter()])

        async def _run():
            with self.assertRaises(RuntimeError):
                await agent.run("hi", thread_id="t_after_fail")

        _aio.run(_run())
        self.assertEqual(calls["after"], 1, "after_agent 不得被重复分发")
        state = agent._states["t_after_fail"]
        self.assertEqual(
            [m.content for m in state.messages], [],
            "收尾失败后本轮消息（user/reply）必须回滚",
        )

    def test_nested_metadata_mutation_rolled_back_on_cancel(self):
        """治理类中间件原地修改嵌套 dict（metadata['governance']['warned']），
        取消后不得残留（快照必须深拷贝）。"""
        import asyncio as _aio

        class GovernanceLike:
            async def abefore_model(self, ctx):
                # 原地修改嵌套结构（不返回 state_patch）
                gov = ctx.state.metadata.setdefault("governance", {})
                gov["warned"] = True
                return None

        async def slow_node(state):
            await _aio.sleep(5)
            return {"messages": [_mkmsg("never")]}

        agent = self._make_agent_with([GovernanceLike()], node_fn=slow_node)

        async def _run():
            task = _aio.create_task(agent.run("hi", thread_id="t_nested"))
            await _aio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(_aio.CancelledError):
                await task

        _aio.run(_run())
        gov = agent._states["t_nested"].metadata.get("governance")
        self.assertFalse(gov and gov.get("warned"), "嵌套 metadata 变更必须被回滚")

    def test_persist_failure_rolls_back_and_restores_counts(self):
        import asyncio as _aio
        import os
        import tempfile
        from novamind.core.state_machine import ConversationStore

        tmp = tempfile.mkdtemp(prefix="novamind_fix1_")
        store = ConversationStore(db_path=os.path.join(tmp, "s.sqlite3"))

        agent = self._make_agent_with([], conversation_store=store)
        # _persist_state 直接打补丁模拟半程提交后失败
        real_persist = agent._persist_state
        calls = {"after": 0}

        class AfterOK:
            async def aafter_agent(self, ctx):
                calls["after"] += 1

        agent._middleware_manager.add_all([AfterOK()])

        def broken_persist(tid, state):
            real_persist(tid, state)  # 先真正写库（模拟半程提交）
            raise RuntimeError("disk full")

        agent._persist_state = broken_persist

        async def _run():
            with self.assertRaises(RuntimeError):
                await agent.run("hi", thread_id="t_persist_fail")

        _aio.run(_run())
        self.assertEqual(calls["after"], 1, "持久化失败不得补发 after_agent")
        state = agent._states["t_persist_fail"]
        self.assertEqual([m.content for m in state.messages], [])
        self.assertEqual(agent._persisted_counts.get("t_persist_fail", 0), 0,
                         "持久化计数必须回滚到轮次前")
        store.close()




class TestAtomicTurnCommit(unittest.TestCase):
    """收尾修复 Phase 1：一轮消息 + 摘要原子提交，失败整批回滚。"""

    def _fresh_store(self):
        import os
        import tempfile
        tmp = tempfile.mkdtemp(prefix="novamind_atomic_")
        return ConversationStore(db_path=os.path.join(tmp, "state.sqlite3")), tmp

    @staticmethod
    def _install_summary_failure(store) -> None:
        """在 summaries 表装 BEFORE INSERT 触发器：任何摘要写入 RAISE(ABORT)。

        故障发生在真实写入事务内部——消息 INSERT 已执行、摘要触发失败，
        事务回滚必须把消息一起撤销。"""
        with store._lock:
            import sqlite3
            conn = sqlite3.connect(store._db_path)
            try:
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS fail_summary
                    BEFORE INSERT ON summaries
                    BEGIN SELECT RAISE(ABORT, 'summary disk full'); END
                """)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _drop_summary_failure(store) -> None:
        with store._lock:
            import sqlite3
            conn = sqlite3.connect(store._db_path)
            try:
                conn.execute("DROP TRIGGER IF EXISTS fail_summary")
                conn.commit()
            finally:
                conn.close()

    def test_save_turn_commit_and_rollback(self):
        """save_turn 消息+摘要同事务；摘要 SQL 失败则消息也不落库。"""
        store, _ = self._fresh_store()
        msgs = [HumanMessage(content="user"), AIMessage(content="reply")]

        store.save_turn("t1", msgs, summary="摘要")
        self.assertEqual([m.content for m in store.load_messages("t1")],
                         ["user", "reply"])
        self.assertEqual(store.load_summary("t1"), "摘要")

        # 摘要写入失败（触发器 RAISE(ABORT) → sqlite3.IntegrityError）：
        # 消息 INSERT 已执行但事务未提交
        import sqlite3
        self._install_summary_failure(store)
        with self.assertRaises(sqlite3.IntegrityError):
            store.save_turn("t2", msgs, summary="s")
        self._drop_summary_failure(store)
        # 整批回滚：t2 无任何消息、无摘要
        self.assertEqual(store.load_messages("t2"), [])
        self.assertEqual(store.load_summary("t2"), "")
        store.close()

    def test_save_turn_summary_none_and_empty(self):
        store, _ = self._fresh_store()
        store.save_turn("t3", [HumanMessage(content="m1")])
        self.assertEqual(store.load_summary("t3"), "")
        store.save_turn("t3", [], summary="")
        self.assertEqual(store.load_summary("t3"), "")
        store.save_turn("t3", [], summary="新摘要")
        self.assertEqual(store.load_summary("t3"), "新摘要")
        store.close()

    def test_persist_failure_rolls_back_and_cross_instance_reload(self):
        """摘要写入失败 → 本轮消息不落库；新 Agent 重载也没有失败轮次。"""
        import os
        import tempfile
        tmp = tempfile.mkdtemp(prefix="novamind_atomic2_")
        db = os.path.join(tmp, "state.sqlite3")
        store = ConversationStore(db_path=db)

        seed = NovaMindAgent(conversation_store=store)
        seed._get_or_create_state("t_atomic").add_message(_mkmsg("old"))
        seed._persist_state("t_atomic", seed._states["t_atomic"])
        self.assertEqual([m.content for m in store.load_messages("t_atomic")], ["old"])

        async def reply_node(s):
            return {"messages": [_mkmsg("reply2")]}

        agent = NovaMindAgent(conversation_store=store)
        agent.add_node("agent", reply_node)
        agent.add_conditional_edge("agent", lambda s: "__end__", {"__end__": "__end__"})

        state = agent._get_or_create_state("t_atomic")
        state.summary = "本轮摘要"
        import sqlite3
        self._install_summary_failure(store)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                asyncio.run(agent._execute_turn(state, "hi", "t_atomic", 50))
        finally:
            self._drop_summary_failure(store)

        # 数据库没有第二轮消息
        self.assertEqual([m.content for m in store.load_messages("t_atomic")], ["old"])
        # 新 Agent 重载结果一致
        reloaded = NovaMindAgent(conversation_store=store)
        st2 = reloaded._get_or_create_state("t_atomic")
        self.assertEqual([m.content for m in st2.messages], ["old"])
        self.assertEqual(reloaded._persisted_counts.get("t_atomic"), 1)
        store.close()

    def test_nested_metadata_mutation_hook_executed_then_rolled_back(self):
        """执行型回归：节点真实 dispatch before_model，hook 原地修改嵌套
        governance dict 后取消，先证明已修改、再证明已还原。"""
        state = AgentState()
        state.metadata["governance"] = {"warned": False, "n": 1}
        changed = asyncio.Event()
        entered = asyncio.Event()

        class GovernanceMutator:
            async def abefore_model(self, ctx):
                gov = ctx.state.metadata["governance"]
                gov["warned"] = True      # 原地修改嵌套 dict
                gov["n"] += 1
                changed.set()
                return None

        agent = NovaMindAgent(
            middleware_manager=MiddlewareManager([GovernanceMutator()])
        )

        async def slow_node(s):
            # 模拟 agent_node 行为：真实 dispatch before_model（hook 在此
            # 原地修改嵌套 governance），再挂起等待取消
            from novamind.core.middlewares import MiddlewareContext

            await agent._middleware_manager.dispatch(
                "before_model", MiddlewareContext(state=s, thread_id="t_gov")
            )
            entered.set()
            await asyncio.sleep(5)
            return {"messages": [_mkmsg("never")]}

        agent.add_node("agent", slow_node)
        agent.add_conditional_edge("agent", lambda s: "__end__", {"__end__": "__end__"})
        agent._states["t_gov"] = state

        async def _run():
            task = asyncio.create_task(agent._execute_turn(state, "hi", "t_gov", 50))
            await changed.wait()
            gov = state.metadata["governance"]
            # 先断言 hook 确实执行并修改了嵌套值
            self.assertTrue(gov["warned"])
            self.assertEqual(gov["n"], 2)
            await entered.wait()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_run())
        # 取消后：嵌套变更被回滚（深拷贝快照）
        gov = agent._states["t_gov"].metadata["governance"]
        self.assertEqual(gov["warned"], False)
        self.assertEqual(gov["n"], 1)
        # 本轮 user 消息也回滚
        self.assertEqual([m.content for m in agent._states["t_gov"].messages], [])

    def test_retry_after_failure_adds_exactly_one_turn(self):
        """故障后重试一次：消息恰好新增一轮，无重复无遗漏。"""
        import os
        import tempfile
        tmp = tempfile.mkdtemp(prefix="novamind_atomic3_")
        db = os.path.join(tmp, "state.sqlite3")
        store = ConversationStore(db_path=db)

        async def reply_node(s):
            # 产出非空摘要：_persist_state 才会写 summaries 触发失败触发器
            return {"messages": [_mkmsg("reply")], "summary": "本轮摘要"}

        agent = NovaMindAgent(conversation_store=store)
        agent.add_node("agent", reply_node)
        agent.add_conditional_edge("agent", lambda s: "__end__", {"__end__": "__end__"})

        # 第一次：摘要触发器失败（整批回滚）；随后 drop 触发器重试成功
        import sqlite3
        self._install_summary_failure(store)
        with self.assertRaises(sqlite3.IntegrityError):
            asyncio.run(agent.run("hi", thread_id="t_retry"))
        self._drop_summary_failure(store)
        # 失败轮已回滚：内存无消息、库无消息
        self.assertEqual([m.content for m in agent._states["t_retry"].messages], [])
        self.assertEqual(store.load_messages("t_retry"), [])
        # 重试成功：恰好一轮 user+reply
        state = asyncio.run(agent.run("hi", thread_id="t_retry"))
        self.assertEqual([m.content for m in state.messages], ["hi", "reply"])
        self.assertEqual([m.content for m in store.load_messages("t_retry")],
                         ["hi", "reply"])
        store.close()


if __name__ == "__main__":
    unittest.main()
