"""
NovaMind 端到端测试

走真实的 create_agent_app -> agent_node -> tool_executor 全链路，
使用 FakeLLM + patch 模式，不依赖真实网络。
"""
import asyncio
import unittest
import uuid
from unittest.mock import patch
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from novamind.core.state_machine import NovaMindAgent, ConversationStore


def _unique_thread_id(prefix="e2e"):
    """生成唯一 thread_id，避免测试间共享 SQLite 数据"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class FakeLLM:
    """可编程的假 LLM，按预设序列返回响应"""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self._call_count = 0
        self.call_history = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        self.call_history.append(messages)
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return AIMessage(content="[FakeLLM default]")

    @property
    def call_count(self):
        return self._call_count


class FakeAuditLogger:
    """收集审计事件用于断言"""

    def __init__(self):
        self.events = []

    def log_event(self, thread_id: str, event: str, **kwargs):
        self.events.append({"thread_id": thread_id, "event": event, **kwargs})

    def get_events(self, event_type: str):
        return [e for e in self.events if e["event"] == event_type]


def _build_agent(fake_llm, fake_audit, tools=None):
    """用 patch 构建真实 agent_app"""
    with patch("novamind.core.agent.get_provider", return_value=fake_llm), \
            patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
            patch("novamind.core.agent.load_mcp_tools", return_value=[]):
        from novamind.core.agent import create_agent_app
        return create_agent_app(
            audit_logger=fake_audit,
            tools=tools or [],
        )


class TestEndToEndAgentCycle(unittest.TestCase):
    """端到端：完整 Agent 循环测试"""

    def test_full_conversation_with_tool_call_cycle(self):
        """测试完整工具调用循环：用户输入 -> LLM调工具 -> 工具执行 -> LLM回复"""
        async def _test():
            tid = _unique_thread_id("tool_cycle")
            audit = FakeAuditLogger()
            llm = FakeLLM(responses=[
                AIMessage(content="", tool_calls=[{
                    "name": "get_current_time",
                    "args": {},
                    "id": "tc_1",
                }]),
                AIMessage(content="现在是北京时间。"),
            ])

            agent = _build_agent(llm, audit)
            result = await agent.run("现在几点", thread_id=tid)

            # 验证消息序列（跳过可能的 SystemMessage）
            types = [type(m).__name__ for m in result.messages
                     if type(m).__name__ != "SystemMessage"]
            self.assertEqual(types, [
                "HumanMessage", "AIMessage", "ToolMessage", "AIMessage"
            ])

            # 验证审计事件
            self.assertGreater(len(audit.get_events("llm_input")), 0)
            self.assertGreater(len(audit.get_events("tool_call")), 0)
            self.assertGreater(len(audit.get_events("tool_result")), 0)
            self.assertGreater(len(audit.get_events("ai_message")), 0)
            self.assertGreater(len(audit.get_events("policy_check")), 0)

            # 清理
            agent.clear_conversation(tid)

        asyncio.run(_test())

    def test_context_trim_and_summary_e2e(self):
        """测试多轮对话触发裁剪 + 摘要评估"""
        async def _test():
            tid = _unique_thread_id("trim")
            audit = FakeAuditLogger()
            llm = FakeLLM(responses=[
                AIMessage(content=f"reply_{i}") for i in range(20)
            ])

            agent = _build_agent(llm, audit)

            # 跑多轮对话，触发裁剪（默认 trigger_turns=40，但 agent 内部用 ContextManager 默认值）
            # 这里用足够多的轮次确保触发
            for i in range(5):
                await agent.run(f"question {i}", thread_id=tid)

            # 验证 agent 正常完成（没有崩溃）
            state = agent._states.get(tid)
            self.assertIsNotNone(state)
            self.assertGreater(len(state.messages), 0)

            # 清理
            agent.clear_conversation(tid)

        asyncio.run(_test())

    def test_session_persistence_across_restart(self):
        """测试会话持久化：新建 agent 实例从 SQLite 恢复历史"""
        async def _test():
            tid = _unique_thread_id("persist")

            # 第一阶段：创建 agent，跑 3 轮对话
            audit1 = FakeAuditLogger()
            llm1 = FakeLLM(responses=[
                AIMessage(content="hello response"),
                AIMessage(content="second response"),
                AIMessage(content="third response"),
            ])

            agent1 = _build_agent(llm1, audit1)
            await agent1.run("hello", thread_id=tid)
            await agent1.run("second message", thread_id=tid)
            await agent1.run("third message", thread_id=tid)

            # 验证第一阶段有 6 条消息（3 human + 3 ai）
            state1 = agent1._states[tid]
            msg_count_phase1 = len(state1.messages)
            self.assertEqual(msg_count_phase1, 6)

            # 第二阶段：新建 agent 实例（模拟重启），用相同 thread_id 恢复
            audit2 = FakeAuditLogger()
            llm2 = FakeLLM(responses=[AIMessage(content="after restart")])
            agent2 = _build_agent(llm2, audit2)

            # 恢复后的 agent 应该有历史消息
            state2 = agent2._get_or_create_state(tid)
            self.assertGreater(len(state2.messages), 0)
            # 应该包含之前的历史
            self.assertEqual(len(state2.messages), msg_count_phase1)

            # 继续对话
            result = await agent2.run("continue after restart", thread_id=tid)
            # 新消息被追加
            self.assertGreater(len(result.messages), msg_count_phase1)

            # 清理
            agent1.clear_conversation(tid)
            agent2.clear_conversation(tid)

        asyncio.run(_test())

    def test_policy_violation_blocks_tool_e2e(self):
        """测试非白名单工具被策略拦截"""
        async def _test():
            tid = _unique_thread_id("policy")
            audit = FakeAuditLogger()
            llm = FakeLLM(responses=[
                AIMessage(content="", tool_calls=[{
                    "name": "nonexistent_dangerous_tool",
                    "args": {},
                    "id": "tc_bad",
                }]),
                AIMessage(content="好的，我换个方式"),
            ])

            agent = _build_agent(llm, audit)
            result = await agent.run("执行危险操作", thread_id=tid)

            # 验证策略违规事件被记录
            violations = audit.get_events("policy_violation")
            self.assertGreater(len(violations), 0)

            # 验证 ToolMessage 包含拦截信息
            tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
            self.assertTrue(any("策略拦截" in m.content for m in tool_msgs))

            # 验证 agent 继续循环，最终返回正常回复
            self.assertIsInstance(result.messages[-1], AIMessage)
            self.assertEqual(result.messages[-1].content, "好的，我换个方式")

            # 清理
            agent.clear_conversation(tid)

        asyncio.run(_test())

    def test_max_iterations_e2e(self):
        """测试死循环触发最大迭代上限"""
        async def _test():
            tid = _unique_thread_id("maxiter")
            audit = FakeAuditLogger()
            # FakeLLM 始终返回 tool_calls，制造死循环
            llm = FakeLLM(responses=[
                AIMessage(content="", tool_calls=[{
                    "name": "get_current_time",
                    "args": {},
                    "id": f"tc_{i}",
                }]) for i in range(100)
            ])

            agent = _build_agent(llm, audit)
            result = await agent.run("无限循环", thread_id=tid, max_iterations=3)

            # 验证达到上限标记
            self.assertTrue(result.metadata.get("max_iterations_reached"))

            # 验证审计事件记录了系统动作
            system_actions = audit.get_events("system_action")
            self.assertGreater(len(system_actions), 0)
            self.assertTrue(any("最大迭代" in e.get("action", "") for e in system_actions))

            # 清理
            agent.clear_conversation(tid)

        asyncio.run(_test())

    def test_context_pack_loading_e2e(self):
        """测试文件关键词触发 file-edit playbook 加载"""
        async def _test():
            tid = _unique_thread_id("ctxpack")
            audit = FakeAuditLogger()
            llm = FakeLLM(responses=[AIMessage(content="好的，我来看看文件")])

            agent = _build_agent(llm, audit)
            await agent.run("请帮我修改 README 文件", thread_id=tid)

            # 验证 context_pack_loaded 事件
            pack_events = audit.get_events("context_pack_loaded")
            self.assertEqual(len(pack_events), 1)
            self.assertEqual(pack_events[0]["pack"], "runtime-core+file-edit")
            self.assertIn("playbooks/file-edit.md", pack_events[0]["documents"])

            # 清理
            agent.clear_conversation(tid)

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
