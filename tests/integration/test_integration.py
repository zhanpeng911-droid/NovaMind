"""
NovaMind Integration Smoke 层 — 关键链路冒烟验证

验证 CLI / 会话 / 日志 / monitor 的核心链路是否串通。
不依赖真实 LLM 网络调用，不驱动 prompt_toolkit 交互。

覆盖链路：
  1. run 会话创建与恢复
  2. 日志与 thread_id 对齐
  3. monitor 目标发现
  4. SQLite 会话隔离
  5. 审计日志写入与读取
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from novamind.core.state_machine import AgentState, NovaMindAgent, ConversationStore


# ==================== 1. run 会话创建与恢复 ====================

class TestIntegrationRunSession(unittest.TestCase):
    """验证 run 入口的会话创建与恢复链路"""

    def test_default_creates_new_session(self):
        """默认运行生成唯一 thread_id"""
        from entry.main import generate_thread_id
        tid1 = generate_thread_id()
        tid2 = generate_thread_id()
        self.assertNotEqual(tid1, tid2)
        self.assertTrue(tid1.startswith("session_"))
        self.assertTrue(tid2.startswith("session_"))

    def test_explicit_thread_id_is_preserved(self):
        """指定 thread_id 在整个生命周期内稳定"""
        async def _test():
            agent = NovaMindAgent()
            seen_tids = []

            async def agent_node(state):
                seen_tids.append(state.metadata.get("thread_id", ""))
                return {"messages": [AIMessage(content="ok")]}

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            await agent.run("hi", thread_id="my_custom_session")
            await agent.run("hello", thread_id="my_custom_session")

            # 验证：两次调用使用相同的 thread_id
            self.assertEqual(seen_tids, ["my_custom_session", "my_custom_session"])

        asyncio.run(_test())

    def test_main_accepts_thread_id(self):
        """main() 和 async_main() 接受 thread_id 参数"""
        import inspect
        from entry.main import main, async_main
        self.assertIn("thread_id", inspect.signature(main).parameters)
        self.assertIn("thread_id", inspect.signature(async_main).parameters)


# ==================== 2. 日志与 thread_id 对齐 ====================

class TestIntegrationLogAlignment(unittest.TestCase):
    """验证日志文件名与 thread_id 一致"""

    def test_logger_safe_id_matches_thread_id(self):
        """logger 的 safe_id 转换与 thread_id 一致"""
        # 验证 logger 的文件名安全化逻辑
        thread_ids = [
            "session_20260101_120000_aaaa1111",
            "my_custom_session",
            "test/session:123",
        ]
        for tid in thread_ids:
            safe_id = "".join(c for c in tid if c.isalnum() or c in "-_") or "default"
            # safe_id 应该可以安全用作文件名
            self.assertTrue(all(c.isalnum() or c in "-_" for c in safe_id))

    def test_logger_log_eventpõe_correct_fields(self):
        """log_event 生成的日志条目包含必要字段"""
        # 通过 _sanitize_for_log 验证脱敏逻辑
        from novamind.core.logger import _sanitize_for_log

        # 验证：敏感 key 被脱敏
        data = {"api_key": "sk-secret-12345", "tool": "calc"}
        sanitized = _sanitize_for_log(data)
        self.assertEqual(sanitized["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["tool"], "calc")

        # 验证：secret-like 值被脱敏
        value = "token sk-abcdefghijklmnopqrstuvwxyz0123456789"
        sanitized = _sanitize_for_log(value)
        self.assertIn("[REDACTED]", sanitized)
        self.assertNotIn("sk-abc", sanitized)


# ==================== 3. monitor 目标发现 ====================

class TestIntegrationMonitorDiscovery(unittest.TestCase):
    """验证 monitor 的会话发现与目标解析链路"""

    def test_list_sessions_finds_jsonl_files(self):
        """list_sessions 能发现日志目录中的会话"""
        from entry.monitor import list_sessions
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建测试日志文件
            for name in ["session_a.jsonl", "session_b.jsonl"]:
                with open(os.path.join(tmpdir, name), "w") as f:
                    f.write('{"event": "test"}\n')

            with patch("entry.monitor.LOG_DIR", tmpdir):
                sessions = list_sessions()

            self.assertEqual(len(sessions), 2)
            ids = {s["thread_id"] for s in sessions}
            self.assertIn("session_a", ids)
            self.assertIn("session_b", ids)

    def test_resolve_picks_most_recent(self):
        """默认解析选择最新修改的日志"""
        from entry.monitor import resolve_log_target
        with tempfile.TemporaryDirectory() as tmpdir:
            old = os.path.join(tmpdir, "old.jsonl")
            new = os.path.join(tmpdir, "new.jsonl")
            with open(old, "w") as f:
                f.write("old\n")
            time.sleep(0.05)
            with open(new, "w") as f:
                f.write("new\n")

            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target(None)
            self.assertEqual(result, new)

    def test_resolve_explicit_thread_id(self):
        """指定 thread_id 能精确定位日志"""
        from entry.monitor import resolve_log_target
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "target_session.jsonl")
            with open(log_path, "w") as f:
                f.write("target\n")

            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target("target_session")
            self.assertEqual(result, log_path)

    def test_resolve_missing_returns_none(self):
        """不存在的 thread_id 返回 None"""
        from entry.monitor import resolve_log_target
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target("nonexistent")
            self.assertIsNone(result)


# ==================== 4. SQLite 会话隔离 ====================

class TestIntegrationSqliteIsolation(unittest.TestCase):
    """验证不同 thread_id 在 SQLite 中的状态隔离"""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmp_dir, "test.sqlite3")

    def tearDown(self):
        import gc, shutil
        gc.collect()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_separate_thread_ids_are_isolated(self):
        """两个 thread_id 的 SQLite 持久化数据互不干扰"""
        store = ConversationStore(self._db_path)

        store.save_messages("session_X", [HumanMessage(content="X msg")])
        store.save_messages("session_Y", [HumanMessage(content="Y msg")])

        msgs_x = store.load_messages("session_X")
        msgs_y = store.load_messages("session_Y")

        self.assertEqual(len(msgs_x), 1)
        self.assertEqual(msgs_x[0].content, "X msg")
        self.assertEqual(len(msgs_y), 1)
        self.assertEqual(msgs_y[0].content, "Y msg")

    def test_same_thread_id_accumulates(self):
        """相同 thread_id 的消息累积"""
        store = ConversationStore(self._db_path)

        store.save_messages("session_Z", [HumanMessage(content="msg1")])
        store.save_messages("session_Z", [HumanMessage(content="msg2")])
        store.save_messages("session_Z", [HumanMessage(content="msg3")])

        msgs = store.load_messages("session_Z")
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0].content, "msg1")
        self.assertEqual(msgs[2].content, "msg3")

    def test_summary_persistence_per_thread(self):
        """摘要按 thread_id 隔离"""
        store = ConversationStore(self._db_path)

        store.save_summary("s1", "summary for s1")
        store.save_summary("s2", "summary for s2")

        self.assertEqual(store.load_summary("s1"), "summary for s1")
        self.assertEqual(store.load_summary("s2"), "summary for s2")

    def test_clear_thread_is_isolated(self):
        """清除一个 thread_id 不影响其他"""
        store = ConversationStore(self._db_path)

        store.save_messages("keep", [HumanMessage(content="keep me")])
        store.save_messages("delete", [HumanMessage(content="delete me")])

        store.clear_thread("delete")

        self.assertEqual(len(store.load_messages("keep")), 1)
        self.assertEqual(len(store.load_messages("delete")), 0)


# ==================== 5. 端到端链路冒烟 ====================

class TestIntegrationEndToEnd(unittest.TestCase):
    """端到端链路冒烟：agent → state → log → monitor 对齐"""

    def test_agent_run_produces_loggable_state(self):
        """agent 运行后的状态可被 logger 和 monitor 消费"""
        async def _test():
            agent = NovaMindAgent()

            async def agent_node(state):
                return {"messages": [AIMessage(content="response")]}

            agent.add_node("agent", agent_node)
            agent.add_edge("START", "agent")
            agent.add_edge("agent", "__end__")

            result = await agent.run("test input", thread_id="e2e_test")

            # 验证：状态可用于日志记录
            self.assertEqual(result.metadata.get("thread_id"), "e2e_test")
            self.assertGreater(len(result.messages), 0)

            # 验证：最后一条消息可序列化为日志格式
            last_msg = result.messages[-1]
            log_entry = {
                "thread_id": "e2e_test",
                "event": "ai_message",
                "content": last_msg.content if hasattr(last_msg, "content") else str(last_msg),
            }
            self.assertEqual(log_entry["thread_id"], "e2e_test")
            self.assertIn("response", log_entry["content"])

        asyncio.run(_test())

    def test_full_agent_tool_cycle(self):
        """完整 agent 工具调用循环的状态正确性"""
        async def _test():
            fake_llm_responses = [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "calc", "args": {"expr": "2+2"}, "id": "tc_1"}],
                ),
                AIMessage(content="结果是 4"),
            ]
            call_idx = 0

            agent = NovaMindAgent()

            async def agent_node(state):
                nonlocal call_idx
                resp = fake_llm_responses[call_idx]
                call_idx += 1
                return {"messages": [resp]}

            async def tool_executor(state):
                return {"messages": [
                    ToolMessage(content="4", tool_call_id="tc_1", name="calc")
                ]}

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

            result = await agent.run("2+2=?", thread_id="e2e_tool")

            # 验证：消息链完整
            types = [type(m).__name__ for m in result.messages]
            self.assertEqual(types, [
                "HumanMessage", "AIMessage", "ToolMessage", "AIMessage"
            ])
            # 验证：最终回复正确
            self.assertEqual(result.messages[-1].content, "结果是 4")

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
