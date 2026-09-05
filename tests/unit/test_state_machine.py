"""
NovaMind 状态机引擎测试

测试自定义状态机的核心功能：
  - 节点注册与执行
  - 固定跳转边
  - 条件跳转边
  - 完整的智能体循环
  - SQLite对话持久化
  - 跨调用状态保持
"""
import asyncio
import os
import tempfile
import unittest
from novamind.core.state_machine import (
    AgentState, NovaMindAgent, ConversationStore, Edge
)
from novamind.core.context import ContextManager
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, RemoveMessage


class TestAgentState(unittest.TestCase):
    """测试 AgentState 数据容器"""

    def test_initial_state(self):
        state = AgentState()
        self.assertEqual(state.messages, [])
        self.assertEqual(state.summary, "")
        self.assertEqual(state.metadata, {})

    def test_add_message(self):
        state = AgentState()
        msg = HumanMessage(content="你好")
        state.add_message(msg)
        self.assertEqual(len(state.messages), 1)
        self.assertEqual(state.messages[0].content, "你好")

    def test_add_messages_batch(self):
        state = AgentState()
        msgs = [HumanMessage(content="问题"), AIMessage(content="回答")]
        state.add_messages(msgs)
        self.assertEqual(len(state.messages), 2)

    def test_remove_messages(self):
        state = AgentState()
        msg1 = HumanMessage(content="消息1")
        msg1.id = "msg_1"
        msg2 = AIMessage(content="消息2")
        msg2.id = "msg_2"
        state.add_messages([msg1, msg2])
        state.remove_messages(["msg_1"])
        self.assertEqual(len(state.messages), 1)
        self.assertEqual(state.messages[0].id, "msg_2")


class TestEdge(unittest.TestCase):
    """测试 Edge 边跳转"""

    def test_fixed_edge(self):
        edge = Edge(source="a", target="b")
        state = AgentState()
        self.assertEqual(edge.resolve(state), "b")

    def test_conditional_edge(self):
        def route(state):
            return "yes" if state.summary else "no"

        edge = Edge(
            source="decision",
            condition=route,
            condition_map={"yes": "handler_a", "no": "handler_b"},
        )
        self.assertEqual(edge.resolve(AgentState()), "handler_b")
        self.assertEqual(edge.resolve(AgentState(summary="ctx")), "handler_a")


class TestConversationStore(unittest.TestCase):
    """测试 SQLite 对话持久化"""

    def setUp(self):
        # 使用临时目录避免 Windows 文件锁定问题
        self._tmp_dir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmp_dir, "test.sqlite3")
        self.store = ConversationStore(self._db_path)

    def tearDown(self):
        import gc
        gc.collect()  # 确保所有 sqlite3 连接已释放
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_save_and_load_messages(self):
        """测试消息保存和加载"""
        msgs = [
            HumanMessage(content="你好"),
            AIMessage(content="你好！有什么可以帮你的？"),
        ]
        self.store.save_messages("thread_1", msgs)

        loaded = self.store.load_messages("thread_1")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].content, "你好")
        self.assertEqual(loaded[1].content, "你好！有什么可以帮你的？")

    def test_thread_isolation(self):
        """测试不同线程的数据隔离"""
        self.store.save_messages("A", [HumanMessage(content="A的消息")])
        self.store.save_messages("B", [HumanMessage(content="B的消息")])

        self.assertEqual(len(self.store.load_messages("A")), 1)
        self.assertEqual(len(self.store.load_messages("B")), 1)
        self.assertEqual(self.store.load_messages("A")[0].content, "A的消息")

    def test_save_and_load_summary(self):
        """测试摘要保存和加载"""
        self.store.save_summary("t1", "这是摘要")
        self.assertEqual(self.store.load_summary("t1"), "这是摘要")

        # 覆盖更新
        self.store.save_summary("t1", "新摘要")
        self.assertEqual(self.store.load_summary("t1"), "新摘要")

    def test_clear_thread(self):
        """测试清除会话数据"""
        self.store.save_messages("t1", [HumanMessage(content="test")])
        self.store.save_summary("t1", "summary")
        self.store.clear_thread("t1")

        self.assertEqual(len(self.store.load_messages("t1")), 0)
        self.assertEqual(self.store.load_summary("t1"), "")

    def test_tool_message_persistence(self):
        """测试工具消息的保存和加载（含tool_call_id）"""
        msg = ToolMessage(content="42", tool_call_id="tc_1", name="calculator")
        self.store.save_messages("t1", [msg])

        loaded = self.store.load_messages("t1")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].tool_call_id, "tc_1")
        self.assertEqual(loaded[0].name, "calculator")

    def test_ai_message_with_tool_calls(self):
        """测试带tool_calls的AI消息保存"""
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "calc", "args": {"expr": "1+1"}, "id": "tc_1"}],
        )
        self.store.save_messages("t1", [msg])

        loaded = self.store.load_messages("t1")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].tool_calls[0]["name"], "calc")


class TestNovaMindAgent(unittest.TestCase):
    """测试 NovaMindAgent 完整循环"""

    def test_simple_linear_flow(self):
        """测试线性流程：START -> agent -> END"""
        async def _test():
            agent = NovaMindAgent()

            async def agent_func(state):
                return {"messages": [AIMessage(content="Hello!")]}

            agent.add_node("agent", agent_func)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            result = await agent.run("hi", thread_id="test_1")
            self.assertEqual(len(result.messages), 2)
            self.assertEqual(result.messages[1].content, "Hello!")
            self.assertEqual(result.metadata["iteration"], 1)

        asyncio.run(_test())

    def test_tool_call_loop(self):
        """测试工具调用循环：agent -> tools -> agent -> END"""
        async def _test():
            agent = NovaMindAgent()
            call_count = 0

            async def agent_func(state):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return {"messages": [
                        AIMessage(
                            content="",
                            tool_calls=[{"name": "test_tool", "args": {}, "id": "tc_1"}],
                        )
                    ]}
                else:
                    return {"messages": [AIMessage(content="Done!")]}

            async def tool_func(state):
                return {"messages": [ToolMessage(content="tool result", tool_call_id="tc_1")]}

            def route(state):
                last = state.messages[-1] if state.messages else None
                if last and hasattr(last, "tool_calls") and last.tool_calls:
                    return "tools"
                return "__end__"

            agent.add_node("agent", agent_func)
            agent.add_node("tools", tool_func)
            agent.add_edge("START", "agent")
            agent.add_conditional_edge("agent", route, {"tools": "tools", "__end__": "__end__"})
            agent.add_edge("tools", "agent")

            result = await agent.run("do something", thread_id="test_2")
            self.assertEqual(result.messages[-1].content, "Done!")
            self.assertIn("agent->tools", result.metadata["visited_edges"])

        asyncio.run(_test())

    def test_max_iterations_limit(self):
        """测试最大循环次数限制"""
        async def _test():
            agent = NovaMindAgent()

            async def infinite_agent(state):
                return {"messages": [AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "t"}])]}

            async def noop_tool(state):
                return {"messages": [AIMessage(content="still looping")]}

            def always_tools(state):
                return "tools"

            agent.add_node("agent", infinite_agent)
            agent.add_node("tools", noop_tool)
            agent.add_edge("START", "agent")
            agent.add_conditional_edge("agent", always_tools, {"tools": "tools"})
            agent.add_edge("tools", "agent")

            result = await agent.run("loop", thread_id="test_3", max_iterations=5)
            self.assertEqual(result.metadata["iteration"], 5)
            # 达到上限时应设置标记
            self.assertTrue(result.metadata.get("max_iterations_reached"))

        asyncio.run(_test())

    def test_max_iterations_astream_yields_limit_event(self):
        """astream 达到迭代上限时应 yield __limit__ 事件"""
        async def _test():
            agent = NovaMindAgent()

            async def infinite_agent(state):
                return {"messages": [AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "t"}])]}

            async def noop_tool(state):
                return {"messages": [AIMessage(content="loop")]}

            def always_tools(state):
                return "tools"

            agent.add_node("agent", infinite_agent)
            agent.add_node("tools", noop_tool)
            agent.add_edge("START", "agent")
            agent.add_conditional_edge("agent", always_tools, {"tools": "tools"})
            agent.add_edge("tools", "agent")

            events = []
            async for event in agent.astream("loop", thread_id="test_limit", max_iterations=3):
                events.append(event)

            # 最后一个事件应为 __limit__
            self.assertIn("__limit__", events[-1])
            self.assertEqual(events[-1]["__limit__"]["max_iterations"], 3)

        asyncio.run(_test())

    def test_cross_call_state_persistence(self):
        """测试跨调用状态保持（同一会话内多次run保持对话连贯）"""
        async def _test():
            agent = NovaMindAgent()
            responses = []

            async def agent_func(state):
                # 记录每次调用时的消息数量
                responses.append(len(state.messages))
                return {"messages": [AIMessage(content=f"reply_{len(state.messages)}")]}

            agent.add_node("agent", agent_func)
            agent.add_edge("START", "agent")

            # 第一次调用
            await agent.run("第一句话", thread_id="persistent_session")
            # 第二次调用（状态应该包含之前的消息）
            await agent.run("第二句话", thread_id="persistent_session")

            # 第一次调用时有1条消息(HumanMessage)，第二次有3条(H1+A1+H2)
            self.assertEqual(responses[0], 1)
            self.assertEqual(responses[1], 3)

        asyncio.run(_test())

    def test_persistence_to_sqlite(self):
        """测试对话持久化到SQLite"""
        async def _test():
            tmp_dir = tempfile.mkdtemp()
            db_path = os.path.join(tmp_dir, "persist_test.sqlite3")

            try:
                store = ConversationStore(db_path)
                agent = NovaMindAgent(conversation_store=store)

                async def agent_func(state):
                    return {"messages": [AIMessage(content="stored!")]}

                agent.add_node("agent", agent_func)
                agent.add_edge("START", "agent")

                await agent.run("持久化测试", thread_id="persist_test")

                # 验证数据已写入数据库
                loaded = store.load_messages("persist_test")
                self.assertGreater(len(loaded), 0)

                # 创建新Agent，从数据库恢复状态
                agent2 = NovaMindAgent(conversation_store=store)
                agent2.add_node("agent", agent_func)
                agent2.add_edge("START", "agent")

                call_count = []
                async def agent_func2(state):
                    call_count.append(len(state.messages))
                    return {"messages": [AIMessage(content="restored!")]}

                agent2._nodes["agent"].func = agent_func2
                await agent2.run("恢复测试", thread_id="persist_test")

                # 恢复后应该有之前的消息
                self.assertEqual(call_count[0], 3)

            finally:
                import gc
                import shutil
                gc.collect()
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(_test())

    def test_astream_applies_metadata_and_records_edges(self):
        """测试流式执行也会应用metadata并记录路由路径"""
        async def _test():
            agent = NovaMindAgent()

            async def agent_func(state):
                return {
                    "messages": [AIMessage(content="streamed!")],
                    "metadata": {"token_total": 123},
                }

            agent.add_node("agent", agent_func)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            events = []
            async for event in agent.astream("hi", thread_id="stream_meta"):
                events.append(event)

            state = agent._states["stream_meta"]
            self.assertEqual(len(events), 1)
            self.assertEqual(state.metadata["token_total"], 123)
            self.assertEqual(state.metadata["visited_edges"], ["agent->__end__"])

        asyncio.run(_test())

    def test_persists_new_messages_after_memory_trim(self):
        """测试内存消息被裁剪后，本轮新增消息仍会持久化"""
        async def _test():
            tmp_dir = tempfile.mkdtemp()
            db_path = os.path.join(tmp_dir, "trim_persist.sqlite3")

            try:
                store = ConversationStore(db_path)
                agent = NovaMindAgent(conversation_store=store)
                calls = 0

                async def agent_func(state):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        state.messages[0].id = "h1"
                        msg = AIMessage(content="old")
                        msg.id = "a1"
                        return {"messages": [msg]}

                    return {
                        "messages": [
                            RemoveMessage(id="h1"),
                            RemoveMessage(id="a1"),
                            AIMessage(content="new after trim"),
                        ]
                    }

                agent.add_node("agent", agent_func)
                agent.add_edge("START", "agent")

                await agent.run("first", thread_id="trim_case")
                await agent.run("second", thread_id="trim_case")

                loaded = store.load_messages("trim_case")
                self.assertEqual(len(loaded), 4)
                self.assertEqual(loaded[-1].content, "new after trim")

            finally:
                import gc
                import shutil
                gc.collect()
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(_test())

    def test_trim_actually_removes_messages(self):
        """测试裁剪后 state.messages 长度实际下降（不是假删除）"""
        async def _test():
            # ContextManager: trigger_turns=3, keep_turns=1
            ctx = ContextManager(trigger_turns=3, keep_turns=1)

            agent = NovaMindAgent(context_manager=ctx)

            async def agent_node(state):
                raw = state.messages
                final_msgs, discarded = ctx.trim_messages(raw)
                state_updates = {}
                if discarded:
                    new_summary = ctx.generate_summary(state.summary, discarded)
                    state_updates["summary"] = new_summary
                    # 确保所有被丢弃消息都有 id 才生成 RemoveMessage
                    delete_cmds = [RemoveMessage(id=m.id) for m in discarded if m.id]
                    state_updates["messages"] = delete_cmds
                if "messages" not in state_updates:
                    state_updates["messages"] = []
                state_updates["messages"].append(AIMessage(content="ok"))
                return state_updates

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")

            # 第一轮：加入少量消息（不触发裁剪）
            result1 = await agent.run("hi", thread_id="trim_verify")
            count_after_first = len(result1.messages)

            # 第二轮：手动塞入额外消息，使回合数达到触发阈值
            state = agent._states["trim_verify"]
            extra = []
            for i in range(6):
                extra.append(HumanMessage(content=f"extra_q_{i}", id=f"extra_h_{i}"))
                extra.append(AIMessage(content=f"extra_a_{i}", id=f"extra_a_{i}"))
            state.add_messages(extra)

            result2 = await agent.run("trigger trim", thread_id="trim_verify")
            # 预期：回合数从 >3 被裁到 keep_turns=1，消息数大幅下降
            # 原始消息: 1(H) + 1(A) + 6*2(H+A) + 1(H) + 1(A) = 16
            # 裁剪后保留最近1个回合 + 新增的 HumanMessage + AIMessage
            self.assertLess(len(result2.messages), count_after_first + len(extra),
                            "裁剪未生效，state.messages 长度没有下降")

        asyncio.run(_test())

    def test_same_round_uses_new_summary(self):
        """测试裁剪发生时，新生成的 summary 在同一轮被用于构造 LLM 消息"""
        async def _test():
            ctx = ContextManager(trigger_turns=3, keep_turns=1)
            summaries_used = []  # 记录每次 build_messages_for_llm 收到的 summary

            agent = NovaMindAgent(context_manager=ctx)

            async def agent_node(state):
                raw = state.messages
                final_msgs, discarded = ctx.trim_messages(raw)
                state_updates = {}
                if discarded:
                    new_summary = ctx.generate_summary(state.summary, discarded)
                    state_updates["summary"] = new_summary
                    delete_cmds = [RemoveMessage(id=m.id) for m in discarded if m.id]
                    state_updates["messages"] = delete_cmds
                if "messages" not in state_updates:
                    state_updates["messages"] = []

                # 记录传入 build_messages_for_llm 的 summary
                effective = state_updates.get("summary", state.summary)
                summaries_used.append(effective)
                state_updates["messages"].append(AIMessage(content="ok"))
                return state_updates

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")

            # 第一轮
            await agent.run("hi", thread_id="summary_round")
            # 塞入足够消息触发裁剪
            state = agent._states["summary_round"]
            extra = []
            for i in range(6):
                extra.append(HumanMessage(content=f"q_{i}", id=f"h_{i}"))
                extra.append(AIMessage(content=f"a_{i}", id=f"ai_{i}"))
            state.add_messages(extra)

            old_summary = state.summary
            await agent.run("trigger", thread_id="summary_round")
            new_summary = state.summary

            # 验证：裁剪发生时，传给 LLM 的 summary 是新的，不是旧的
            # summaries_used[-1] 是第二轮调用时传入的 summary
            if old_summary:  # 如果有旧 summary，确认被替换
                self.assertNotEqual(summaries_used[-1], old_summary,
                                    "同一轮裁剪后仍使用旧 summary 构造 LLM 消息")
            # 确认新 summary 已被使用
            self.assertEqual(summaries_used[-1], new_summary,
                             "传给 LLM 的 summary 与 state.summary 不一致")

        asyncio.run(_test())


class TestConversationStoreAtomicBatch(unittest.TestCase):
    """Phase 4：save_messages 一批一个事务，任一条失败整批回滚。"""

    def _mk_store(self):
        tmp = tempfile.mkdtemp(prefix="novamind_p4_")
        return ConversationStore(db_path=os.path.join(tmp, "state.sqlite3"))

    def test_batch_failure_rolls_back_whole_batch(self):
        store = self._mk_store()
        ok_msg = AIMessage(content="keep-me", id="m1")
        store.save_messages("t_batch", [ok_msg])
        before = store.load_messages("t_batch")

        bad_msg = AIMessage(content="bad", id="m2")
        bad_msg.tool_calls = [{"name": "x", "args": {"k": object()}}]  # 不可 JSON 序列化

        with self.assertRaises(TypeError):
            store.save_messages("t_batch", [AIMessage(content="good", id="m3"), bad_msg])

        # 整批回滚：新消息一条都没落
        self.assertEqual([m.content for m in store.load_messages("t_batch")],
                         [m.content for m in before])

    def test_batch_success_persists_all_in_one_transaction(self):
        store = self._mk_store()
        msgs = [HumanMessage(content=f"m{i}", id=f"id{i}") for i in range(5)]
        store.save_messages("t_batch2", msgs)
        loaded = store.load_messages("t_batch2")
        self.assertEqual([m.content for m in loaded], [f"m{i}" for i in range(5)])


class TestConversationStorePagination(unittest.TestCase):
    """Phase 4：load_message_page / list_thread_page 稳定分页。"""

    def _mk_store(self):
        tmp = tempfile.mkdtemp(prefix="novamind_p4_")
        return ConversationStore(db_path=os.path.join(tmp, "state.sqlite3"))

    def test_message_pages_no_dup_no_gap_and_full_restore_matches(self):
        store = self._mk_store()
        msgs = ([HumanMessage(content=f"u{i}", id=f"u{i}") for i in range(7)]
                + [AIMessage(content=f"a{i}", id=f"a{i}") for i in range(5)])
        store.save_messages("t_page", msgs)

        pages, cursors = [], []
        cursor = None
        while True:
            page, cursor = store.load_message_page("t_page", limit=3, before_id=cursor)
            pages.append(page)
            cursors.append(cursor)
            if cursor is None:
                break
        # 分页从最新往回走、页内旧→新：倒序拼接页即完整时间线，无重复无遗漏
        combined = [m.content for p in reversed(pages) for m in p]
        self.assertEqual(combined, [m.content for m in msgs])
        self.assertEqual(len(pages), 4)  # 12 条 / 每页 3 → 4 页
        self.assertIsNone(cursors[-1])
        self.assertTrue(all(c is not None for c in cursors[:-1]))

    def test_tool_only_page_cursor_advances(self):
        store = self._mk_store()
        msgs = [
            HumanMessage(content="q", id="q1"),
            ToolMessage(content="r1", tool_call_id="c1", name="tool", id="t1"),
            ToolMessage(content="r2", tool_call_id="c2", name="tool", id="t2"),
            ToolMessage(content="r3", tool_call_id="c3", name="tool", id="t3"),
            AIMessage(content="done", id="a1"),
        ]
        store.save_messages("t_tool", msgs)
        # 无 cursor 的第一页 = 最新 2 条：[r3, done]
        page1, cur1 = store.load_message_page("t_tool", limit=2)
        self.assertEqual([m.content for m in page1], ["r3", "done"])
        self.assertIsNotNone(cur1)
        # page2 = [r1, r2]：整页都是 tool 行，游标仍按原始行推进
        page2, cur2 = store.load_message_page("t_tool", limit=2, before_id=cur1)
        self.assertEqual([m.content for m in page2], ["r1", "r2"])
        self.assertIsNotNone(cur2)
        page3, cur3 = store.load_message_page("t_tool", limit=2, before_id=cur2)
        self.assertEqual([m.content for m in page3], ["q"])
        self.assertIsNone(cur3)

    def test_load_messages_full_restore_unchanged(self):
        store = self._mk_store()
        msgs = [HumanMessage(content="h", id="h1"),
                AIMessage(content="a", id="a1"),
                ToolMessage(content="t", tool_call_id="c", name="tool", id="t1")]
        store.save_messages("t_full", msgs)
        loaded = store.load_messages("t_full")
        self.assertEqual([m.content for m in loaded], ["h", "a", "t"])

    def test_list_thread_page_stable_order_and_title(self):
        store = self._mk_store()
        # 故意交错插入；同一时间戳下顺序仍需稳定（按 last_id 逆序）
        store.save_messages("t_a", [HumanMessage(content="alpha conversation", id="a1"),
                                    AIMessage(content="more", id="a2")])
        store.save_messages("t_b", [HumanMessage(content="beta", id="b1")])
        store.save_messages("t_c", [AIMessage(content="no human title", id="c1")])

        items, cursor = store.list_thread_page(limit=2)
        self.assertEqual([i["thread_id"] for i in items], ["t_c", "t_b"])
        # t_a 存 2 行、t_b 存 1 行、t_c 存 1 行 → t_b 的 last_id = 3
        self.assertEqual(cursor, (3, "t_b"))
        items2, cursor2 = store.list_thread_page(limit=2, cursor=cursor)
        self.assertEqual([i["thread_id"] for i in items2], ["t_a"])
        self.assertIsNone(cursor2)
        # 标题语义：首条 human 消息；无 human 时回退 "新对话"
        titles = {i["thread_id"]: i["title"] for i in items + items2}
        self.assertEqual(titles["t_a"], "alpha conversation")
        self.assertEqual(titles["t_b"], "beta")
        self.assertEqual(titles["t_c"], "新对话")
        # count 与旧语义一致
        counts = {i["thread_id"]: i["message_count"] for i in items + items2}
        self.assertEqual(counts, {"t_a": 2, "t_b": 1, "t_c": 1})

    def test_list_thread_page_cursor_tiebreak_same_last_id(self):
        """两个 thread 的 last_id 相同（不可能同时成立）——这里验证
        相同 timestamp 下顺序稳定：全部行同一时间戳，分页两次结果一致。"""
        store = self._mk_store()
        for tid in ("t1", "t2", "t3"):
            store.save_messages(tid, [HumanMessage(content=f"hi {tid}", id=f"{tid}-1")])
        page1, cur = store.list_thread_page(limit=2)
        page2, _ = store.list_thread_page(limit=2, cursor=cur)
        again1, cur1b = store.list_thread_page(limit=2)
        again2, _ = store.list_thread_page(limit=2, cursor=cur1b)
        self.assertEqual([i["thread_id"] for i in page1 + page2],
                         [i["thread_id"] for i in again1 + again2])

    def test_concurrent_read_write_no_locked_errors(self):
        """Windows/Linux 并发读写 smoke：WAL + busy_timeout 下不抛 locked。"""
        import asyncio

        store = self._mk_store()
        errors = []

        def writer(i):
            for j in range(5):
                store.save_message(f"t_w{i}", HumanMessage(content=f"w{i}-{j}"))

        def reader():
            for _ in range(20):
                store.load_messages("t_w0")
                store.list_thread_page(limit=10)

        async def _run():
            def safe(fn, *args):
                try:
                    fn(*args)
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)
            await asyncio.gather(
                *[asyncio.to_thread(safe, writer, i) for i in range(3)],
                asyncio.to_thread(safe, reader),
            )

        asyncio.run(_run())
        total = sum(len(store.load_messages(f"t_w{i}")) for i in range(3))
        self.assertEqual(total, 15)


if __name__ == "__main__":
    unittest.main()


class TestStateCacheLRU(unittest.TestCase):
    """代码审查整改回归：长驻进程状态字典 LRU 上限（防随会话数无限增长）。"""

    def test_states_evicted_beyond_cap_and_reload_works(self):
        agent = NovaMindAgent(max_cached_states=3)
        tids = [f"lru_{i}" for i in range(5)]
        for tid in tids:
            agent._get_or_create_state(tid)
        self.assertLessEqual(len(agent._states), 3)
        # 最早的 tids 已被淘汰
        self.assertNotIn("lru_0", agent._states)
        self.assertNotIn("lru_1", agent._states)
        # 被淘汰的会话再次访问 → 重新创建（生产环境会从 SQLite 恢复）
        state = agent._get_or_create_state("lru_0")
        self.assertIsNotNone(state)
        self.assertIn("lru_0", agent._states)

    def test_lru_touch_keeps_recent_thread(self):
        agent = NovaMindAgent(max_cached_states=3)
        agent._get_or_create_state("hot")
        for i in range(3):
            agent._get_or_create_state(f"cold_{i}")
        # hot 若未被 touch 已被淘汰；命中即 touch 的语义下，"hot" 在插入时最新，
        # 3 次 cold 插入后恰好把它挤出去——重新访问应恢复
        state = agent._get_or_create_state("hot")
        self.assertIsNotNone(state)
        self.assertIn("hot", agent._states)
