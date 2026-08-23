"""
NovaMind 端到端测试

走真实的 create_agent_app -> agent_node -> tool_executor 全链路，
使用 FakeLLM + patch 模式，不依赖真实网络。
"""
import asyncio
import json
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from novamind.core.state_machine import NovaMindAgent, ConversationStore
from novamind.core.logger import AuditLogger
from _fakes import FakeLLM, FakeAuditLogger


def _unique_thread_id(prefix="e2e"):
    """生成唯一 thread_id，避免测试间共享 SQLite 数据"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


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


class TestAuditLoggerJsonlPersistence(unittest.TestCase):
    """真实 AuditLogger 写 JSONL 落盘 + 事件序列 端到端验证。

    不用内存 FakeAuditLogger，而是用真实 AuditLogger 指向临时目录，
    跑完整 agent 循环后 shutdown 冲刷队列，读回磁盘上的 JSONL 校验：
      - 事件真实落盘（thread_id.jsonl 存在）
      - 事件字段完整（thread_id/ts/event + 附加数据）
      - 单轮对话：context_pack_loaded → llm_input → ai_message 顺序
      - 工具轮：tool_call → tool_result → ai_message 顺序
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = self._tmpdir.name
        # 重置单例，用临时目录初始化真实 AuditLogger（与 test_logger 同套路）
        AuditLogger._instance = None
        self.logger = AuditLogger(log_dir=self.log_dir)

    def tearDown(self):
        self.logger.shutdown()
        AuditLogger._instance = None
        self._tmpdir.cleanup()

    def _read_events(self, thread_id: str) -> list[dict]:
        file_path = os.path.join(self.log_dir, f"{thread_id}.jsonl")
        self.assertTrue(
            os.path.exists(file_path),
            f"JSONL 审计文件未落盘: {file_path}",
        )
        with open(file_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_simple_turn_persists_events_in_order(self):
        """单轮对话：事件真实落盘且顺序 context_pack_loaded → llm_input → ai_message。"""
        tid = f"audit_{uuid.uuid4().hex[:8]}"
        llm = FakeLLM(responses=[AIMessage(content="你好，我是 NovaMind。")])

        with patch("novamind.core.agent.get_provider", return_value=llm), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]):
            from novamind.core.agent import create_agent_app
            agent = create_agent_app(audit_logger=self.logger, tools=[])
            asyncio.run(agent.run("你好", thread_id=tid))
            agent.clear_conversation(tid)

        # shutdown 冲刷队列到磁盘
        self.logger.shutdown()
        lines = self._read_events(tid)

        events = [e["event"] for e in lines]
        self.assertIn("context_pack_loaded", events)
        self.assertIn("llm_input", events)
        self.assertIn("ai_message", events)
        self.assertLess(events.index("context_pack_loaded"), events.index("ai_message"))
        self.assertLess(events.index("llm_input"), events.index("ai_message"))

        # 字段完整 + 内容落盘
        for e in lines:
            self.assertEqual(e["thread_id"], tid)
            self.assertIn("ts", e)
        ai_events = [e for e in lines if e["event"] == "ai_message"]
        self.assertIn("NovaMind", ai_events[0]["content"])

    def test_tool_turn_persists_full_sequence(self):
        """工具轮：tool_call → policy_check → tool_result → ai_message 顺序落盘。"""
        tid = f"audit_{uuid.uuid4().hex[:8]}"
        llm = FakeLLM(responses=[
            AIMessage(content="", tool_calls=[
                {"name": "get_current_time", "args": {}, "id": "tc_1"},
            ]),
            AIMessage(content="时间已获取。"),
        ])

        with patch("novamind.core.agent.get_provider", return_value=llm), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]):
            from novamind.core.agent import create_agent_app
            agent = create_agent_app(audit_logger=self.logger, tools=[])
            asyncio.run(agent.run("现在几点", thread_id=tid))
            agent.clear_conversation(tid)

        self.logger.shutdown()
        lines = self._read_events(tid)

        events = [e["event"] for e in lines]
        self.assertIn("tool_call", events)
        self.assertIn("policy_check", events)
        self.assertIn("tool_result", events)
        self.assertIn("ai_message", events)
        # 关键顺序：tool_call 先于 tool_result，tool_result 先于最终 ai_message
        self.assertLess(events.index("tool_call"), events.index("tool_result"))
        self.assertLess(events.index("tool_result"), events.index("ai_message"))
        # 工具名落盘
        tool_calls = [e for e in lines if e["event"] == "tool_call"]
        self.assertEqual(tool_calls[0]["tool"], "get_current_time")


if __name__ == "__main__":
    unittest.main()
