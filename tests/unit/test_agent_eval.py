"""
NovaMind Agent Eval 层 — 行为级验证

用 fake LLM 验证 agent 核心行为闭环，不依赖真实网络。
覆盖场景：
  1. 纯回答路径：用户消息 → agent 回复
  2. 工具调用路径：agent 触发 tool_calls → 工具执行 → ToolMessage 回流 → 收敛回复
  3. 长对话裁剪路径：超过阈值 → 旧消息删除 → summary 更新
  4. 会话隔离路径：不同 thread_id 状态互不污染
  5. provider 错误路径：缺依赖 / 缺 key 报错清晰
  6. sandbox 拒绝路径：越权访问被拒绝
"""
import asyncio
import unittest
from unittest.mock import patch

from langchain_core.messages import (
    HumanMessage, AIMessage, ToolMessage, RemoveMessage
)
from novamind.core.state_machine import NovaMindAgent
from novamind.core.context import ContextManager
from _fakes import FakeLLM, env_without


# ==================== Agent Eval 测试 ====================

class TestAgentEvalSimpleResponse(unittest.TestCase):
    """验证纯回答路径：用户消息 → agent 正常返回 AIMessage"""

    def test_single_turn_response(self):
        """单轮对话：输入 → 回复"""
        async def _test():
            fake_llm = FakeLLM(responses=[
                AIMessage(content="你好！有什么可以帮你的？"),
            ])

            agent = NovaMindAgent()

            async def agent_node(state):
                response = fake_llm.invoke(state.messages)
                return {"messages": [response]}

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            result = await agent.run("你好", thread_id="eval_simple")

            # 验证：状态中有用户消息 + AI回复
            self.assertEqual(len(result.messages), 2)
            self.assertIsInstance(result.messages[0], HumanMessage)
            self.assertEqual(result.messages[0].content, "你好")
            self.assertIsInstance(result.messages[1], AIMessage)
            self.assertEqual(result.messages[1].content, "你好！有什么可以帮你的？")

        asyncio.run(_test())

    def test_multi_turn_conversation(self):
        """多轮对话：状态正确累积"""
        async def _test():
            fake_llm = FakeLLM(responses=[
                AIMessage(content="回复1"),
                AIMessage(content="回复2"),
                AIMessage(content="回复3"),
            ])

            agent = NovaMindAgent()
            turn_counts = []

            async def agent_node(state):
                response = fake_llm.invoke(state.messages)
                return {"messages": [response]}

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            await agent.run("msg1", thread_id="eval_multi")
            turn_counts.append(len(agent._states["eval_multi"].messages))

            await agent.run("msg2", thread_id="eval_multi")
            turn_counts.append(len(agent._states["eval_multi"].messages))

            await agent.run("msg3", thread_id="eval_multi")
            turn_counts.append(len(agent._states["eval_multi"].messages))

            # 验证：消息数逐步增长（2, 4, 6）
            self.assertEqual(turn_counts, [2, 4, 6])

        asyncio.run(_test())


class TestAgentEvalToolCall(unittest.TestCase):
    """验证工具调用路径：agent 触发工具 → 工具执行 → 结果回流 → 收敛"""

    def test_tool_call_and_converge(self):
        """完整工具调用循环：agent → tools → agent → END"""
        async def _test():
            fake_llm = FakeLLM(responses=[
                # 第1次：触发工具调用
                AIMessage(
                    content="",
                    tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "tc_1"}],
                ),
                # 第2次：收到工具结果后收敛
                AIMessage(content="计算结果是 2"),
            ])

            agent = NovaMindAgent()

            # 模拟工具执行
            async def tool_executor(state):
                last_msg = state.messages[-1]
                if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                    return {"messages": []}
                tool_messages = []
                for tc in last_msg.tool_calls:
                    # 模拟 calculator 工具返回
                    tool_messages.append(ToolMessage(
                        content="2",
                        tool_call_id=tc.get("id", ""),
                        name=tc["name"],
                    ))
                return {"messages": tool_messages}

            async def agent_node(state):
                response = fake_llm.invoke(state.messages)
                return {"messages": [response]}

            def route(state):
                last = state.messages[-1] if state.messages else None
                if last and isinstance(last, AIMessage) and last.tool_calls:
                    return "tools"
                return "__end__"

            agent.add_node("agent", agent_node)
            agent.add_node("tools", tool_executor)
            agent.add_edge("START", "agent")
            agent.add_conditional_edge("agent", route, {"tools": "tools", "__end__": "__end__"})
            agent.add_edge("tools", "agent")

            result = await agent.run("计算 1+1", thread_id="eval_tool")

            # 验证：消息流包含 HumanMessage、AIMessage(tool_calls)、ToolMessage、AIMessage(最终回复)
            msg_types = [type(m).__name__ for m in result.messages]
            self.assertIn("HumanMessage", msg_types)
            self.assertIn("AIMessage", msg_types)
            self.assertIn("ToolMessage", msg_types)

            # 最后一条是收敛回复
            last_msg = result.messages[-1]
            self.assertIsInstance(last_msg, AIMessage)
            self.assertEqual(last_msg.content, "计算结果是 2")

            # 工具调用次数验证
            self.assertEqual(fake_llm._call_count, 2)

        asyncio.run(_test())

    def test_tool_call_error_handling(self):
        """工具执行异常时，agent 仍能收敛"""
        async def _test():
            fake_llm = FakeLLM(responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "bad_tool", "args": {}, "id": "tc_err"}],
                ),
                AIMessage(content="工具出了点问题，但我还能回复你"),
            ])

            agent = NovaMindAgent()

            async def tool_executor(state):
                last_msg = state.messages[-1]
                if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                    return {"messages": []}
                return {"messages": [
                    ToolMessage(
                        content="工具执行异常: something went wrong",
                        tool_call_id="tc_err",
                        name="bad_tool",
                    )
                ]}

            async def agent_node(state):
                response = fake_llm.invoke(state.messages)
                return {"messages": [response]}

            def route(state):
                last = state.messages[-1] if state.messages else None
                if last and isinstance(last, AIMessage) and last.tool_calls:
                    return "tools"
                return "__end__"

            agent.add_node("agent", agent_node)
            agent.add_node("tools", tool_executor)
            agent.add_edge("START", "agent")
            agent.add_conditional_edge("agent", route, {"tools": "tools", "__end__": "__end__"})
            agent.add_edge("tools", "agent")

            result = await agent.run("执行坏工具", thread_id="eval_tool_err")

            # 验证：最终仍能收敛
            last_msg = result.messages[-1]
            self.assertIsInstance(last_msg, AIMessage)
            self.assertIn("回复你", last_msg.content)

        asyncio.run(_test())


class TestAgentEvalTrimPath(unittest.TestCase):
    """验证长对话裁剪路径：超过阈值 → 旧消息删除 → summary 更新"""

    def test_trim_removes_old_messages(self):
        """触发 trim 后，旧消息被真正移除"""
        async def _test():
            # 低阈值：3 轮触发，保留 1 轮
            ctx = ContextManager(trigger_turns=3, keep_turns=1)

            agent = NovaMindAgent(context_manager=ctx)
            call_count = 0

            async def agent_node(state):
                nonlocal call_count
                call_count += 1
                raw = state.messages
                final_msgs, discarded = ctx.trim_messages(raw)
                state_updates = {}
                if discarded:
                    # 无 LLM 时用简单摘要
                    new_summary = ctx.generate_summary(state.summary, discarded)
                    state_updates["summary"] = new_summary
                    delete_cmds = [RemoveMessage(id=m.id) for m in discarded if m.id]
                    state_updates["messages"] = delete_cmds
                if "messages" not in state_updates:
                    state_updates["messages"] = []
                state_updates["messages"].append(AIMessage(content=f"reply_{call_count}"))
                return state_updates

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")

            # 塞入足够消息触发 trim
            state = agent._get_or_create_state("eval_trim")
            extra = []
            for i in range(6):
                extra.append(HumanMessage(content=f"q_{i}", id=f"h_{i}"))
                extra.append(AIMessage(content=f"a_{i}", id=f"ai_{i}"))
            state.add_messages(extra)

            result = await agent.run("trigger", thread_id="eval_trim")

            # 验证：消息数比原始 12+2=14 条大幅下降
            self.assertLess(len(result.messages), 10,
                            "裁剪未生效，消息数未下降")

        asyncio.run(_test())

    def test_trim_summary_is_used_immediately(self):
        """裁剪发生时，新 summary 在同一轮被使用"""
        async def _test():
            ctx = ContextManager(trigger_turns=3, keep_turns=1)
            summaries_used = []

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
                effective = state_updates.get("summary", state.summary)
                summaries_used.append(effective)
                state_updates["messages"].append(AIMessage(content="ok"))
                return state_updates

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")

            # 第一轮
            await agent.run("hi", thread_id="eval_summary")
            # 塞入触发 trim
            state = agent._states["eval_summary"]
            extra = []
            for i in range(6):
                extra.append(HumanMessage(content=f"q_{i}", id=f"h_{i}"))
                extra.append(AIMessage(content=f"a_{i}", id=f"ai_{i}"))
            state.add_messages(extra)

            old_summary = state.summary
            await agent.run("trigger", thread_id="eval_summary")
            new_summary = state.summary

            # 验证：传给 LLM 的 summary 是新的
            if old_summary:
                self.assertNotEqual(summaries_used[-1], old_summary)
            self.assertEqual(summaries_used[-1], new_summary)

        asyncio.run(_test())


class TestAgentEvalSessionIsolation(unittest.TestCase):
    """验证会话隔离：不同 thread_id 状态互不污染"""

    def test_different_thread_ids_are_isolated(self):
        """两个 thread_id 的状态独立"""
        async def _test():
            agent = NovaMindAgent()
            responses_a = []
            responses_b = []

            async def agent_node(state):
                tid = state.metadata.get("thread_id", "")
                reply = f"reply_for_{tid}"
                if tid == "session_A":
                    responses_a.append(len(state.messages))
                elif tid == "session_B":
                    responses_b.append(len(state.messages))
                return {"messages": [AIMessage(content=reply)]}

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            # 交替运行两个会话
            await agent.run("A1", thread_id="session_A")
            await agent.run("B1", thread_id="session_B")
            await agent.run("A2", thread_id="session_A")
            await agent.run("B2", thread_id="session_B")

            # 验证：每个会话独立计数
            self.assertEqual(responses_a, [1, 3])  # A: 1条H, 然后 3条(H+A+H)
            self.assertEqual(responses_b, [1, 3])  # B: 同理

            # 验证：两个会话的消息内容不交叉
            state_a = agent._states["session_A"]
            state_b = agent._states["session_B"]
            a_contents = [m.content for m in state_a.messages]
            b_contents = [m.content for m in state_b.messages]
            self.assertIn("A1", str(a_contents))
            self.assertNotIn("B1", str(a_contents))
            self.assertIn("B1", str(b_contents))
            self.assertNotIn("A1", str(b_contents))

        asyncio.run(_test())

    def test_same_thread_id_resumes_context(self):
        """相同 thread_id 可恢复上下文"""
        async def _test():
            agent = NovaMindAgent()
            seen_counts = []

            async def agent_node(state):
                seen_counts.append(len(state.messages))
                return {"messages": [AIMessage(content="ok")]}

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            await agent.run("first", thread_id="resume_test")
            await agent.run("second", thread_id="resume_test")
            await agent.run("third", thread_id="resume_test")

            # 验证：每次调用看到的消息数递增
            self.assertEqual(seen_counts, [1, 3, 5])

        asyncio.run(_test())


class TestAgentEvalProviderErrors(unittest.TestCase):
    """验证 provider 错误路径：缺依赖 / 缺 key 报错清晰"""

    def test_unsupported_provider_raises_valueerror(self):
        """不支持的 provider 名称抛出 ValueError"""
        from novamind.core.provider import get_provider
        with self.assertRaises(ValueError) as ctx:
            get_provider(provider_name="nonexistent", model_name="test")
        self.assertIn("不支持的模型提供商", str(ctx.exception))

    def test_openai_missing_key_raises_valueerror(self):
        """OpenAI 缺 API key 抛出 ValueError"""
        from novamind.core.provider import get_provider
        # 只剔除 OPENAI_* 变量，保留系统变量（清空整个 environ 会破坏
        # uv 独立版 Python 的 OpenSSL 初始化，见 _fakes.env_without 注释）
        with patch.dict("os.environ", env_without("OPENAI_"), clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_provider(provider_name="openai", model_name="gpt-4o-mini")
            self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_anthropic_missing_key_raises_valueerror(self):
        """Anthropic 缺 API key 抛出 ValueError"""
        from novamind.core.provider import get_provider
        with patch.dict("os.environ", env_without("ANTHROPIC_"), clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_provider(provider_name="anthropic", model_name="claude-3-haiku")
            self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))


class TestAgentEvalSandboxRejection(unittest.TestCase):
    """验证 sandbox 拒绝路径：越权访问被拒绝"""

    def test_path_traversal_rejected(self):
        """路径穿越攻击被拦截"""
        from novamind.core.tools.sandbox_tools import write_office_file
        result = write_office_file.invoke({
            "filepath": "../../etc/passwd",
            "content": "hacked",
        })
        self.assertIn("越权拦截", result)

    def test_shell_escape_rejected(self):
        """Shell 解释器逃逸被拦截"""
        from novamind.core.tools.sandbox_tools import execute_office_shell
        result = execute_office_shell.invoke({
            "command": "python -c 'import os; os.system(\"rm -rf /\")'",
        })
        self.assertIn("权限拒绝", result)

    def test_safe_shell_allowed(self):
        """安全的 shell 命令被允许"""
        from novamind.core.tools.sandbox_tools import execute_office_shell
        result = execute_office_shell.invoke({"command": "echo hello"})
        self.assertIn("[STDOUT]", result)


if __name__ == "__main__":
    unittest.main()
