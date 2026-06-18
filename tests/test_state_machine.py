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
    AgentState, NovaMindAgent, ConversationStore, Node, Edge
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
                import gc, shutil
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
                import gc, shutil
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


if __name__ == "__main__":
    unittest.main()
