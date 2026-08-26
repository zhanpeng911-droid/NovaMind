"""
WebUI 端点错误分支 + app.py 启动辅助单元测试（P1 覆盖率盲区补齐）。

覆盖关键不变量：
  - /chat 参数校验失败 → 422
  - /monitor/events：thread_id 安全化、缺失文件返空、坏 JSON 行跳过
  - /history：未知会话返空、tool 消息过滤、tool_calls 提取为标签
  - /skills store 异常 → error 字段而非 500
  - app._wait_for_server：就绪返回 / 超时抛 TimeoutError
"""
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from novamind.webui.app import _wait_for_server
from novamind.webui.server import _safe_id, app


class TestChatValidation(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_chat_missing_field_returns_422(self):
        resp = self.client.post("/chat", json={})
        self.assertEqual(resp.status_code, 422)


class TestMonitorEvents(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch("novamind.webui.server.LOG_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_safe_id_strips_path_traversal(self):
        # 只保留字母数字-_，路径分隔符与特殊字符一律剔除
        for bad in ("../../etc/passwd", "a/b\\c", "x y!z"):
            sid = _safe_id(bad)
            for ch in ("/", "\\", " ", "!"):
                self.assertNotIn(ch, sid)

    def test_missing_thread_returns_empty_events(self):
        resp = self.client.get("/monitor/events/no_such_thread")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"events": []})

    def test_bad_json_lines_skipped(self):
        log_path = f"{self.tmp.name}/{_safe_id('t1')}.jsonl"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write('{"event": "ok"}\n')
            f.write("not-json\n")
            f.write("\n")
            f.write('{"event": "second"}\n')
        resp = self.client.get("/monitor/events/t1")
        events = resp.json()["events"]
        self.assertEqual([e["event"] for e in events], ["ok", "second"])


class TestHistoryEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_history_unknown_thread_returns_empty(self):
        with patch("novamind.webui.server.get_history_store") as gh:
            gh.return_value.load_messages.return_value = []
            resp = self.client.get("/history/ghost")
        self.assertEqual(resp.json(), {"messages": []})

    def test_tool_messages_filtered_and_tool_calls_extracted(self):
        from langchain_core.messages import AIMessage, HumanMessage

        human = HumanMessage(content="你好")
        ai = AIMessage(content="我来处理",
                       tool_calls=[{"name": "calculator", "args": {}, "id": "1"}])
        with patch("novamind.webui.server.get_history_store") as gh:
            gh.return_value.load_messages.return_value = [human, ai]
            resp = self.client.get("/history/t1")
        msgs = resp.json()["messages"]
        roles = [m["role"] for m in msgs]
        self.assertNotIn("tool", roles)
        ai_item = next(m for m in msgs if m["role"] == "ai")
        self.assertEqual(ai_item["tools"], ["calculator"])

    def test_store_failure_returns_empty_messages(self):
        with patch("novamind.webui.server.get_history_store",
                   side_effect=RuntimeError("db down")):
            resp = self.client.get("/history/t1")
        self.assertEqual(resp.json(), {"messages": []})


class TestSkillsEndpointError(unittest.TestCase):
    def test_store_exception_returns_error_payload_not_500(self):
        client = TestClient(app)
        with patch("novamind.webui.server.get_skill_store",
                   side_effect=RuntimeError("skill db locked")):
            resp = client.get("/skills")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["skills"], [])
        self.assertIn("error", body)


class TestWaitForServer(unittest.TestCase):
    def test_ready_server_returns_quickly(self):
        """urlopen 返回 200 → 立即就绪返回。"""
        resp = MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        with patch("novamind.webui.app.urllib.request.urlopen", return_value=resp) as up:
            _wait_for_server("http://127.0.0.1:8765", timeout=3.0)
        self.assertTrue(up.called)

    def test_timeout_raises_after_deadline(self):
        """持续连接失败 → 超时抛 TimeoutError。"""
        with patch("novamind.webui.app.urllib.request.urlopen",
                   side_effect=ConnectionRefusedError("refused")), \
             patch("novamind.webui.app.time.sleep") as sleep_mock:
            with self.assertRaises(TimeoutError):
                _wait_for_server("http://127.0.0.1:1", timeout=0.2)
        self.assertTrue(sleep_mock.called)


if __name__ == "__main__":
    unittest.main()
