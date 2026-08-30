"""
MCP 适配器单元测试（代码审查整改：响应 id 关联 / 错误不缓存 / 每服务独立 IO 池）。
用 Fake 进程对象模拟 stdio，不启动真实子进程。
"""
import json
import unittest

from novamind.core.mcp_adapter import MCPService


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stdin = _FakeStdin()
        self.poll = lambda: None  # 模拟存活


def _service(lines) -> MCPService:
    svc = MCPService("t", "cmd")
    svc._started = True
    svc._process = _FakeProc(lines)
    return svc


class TestResponseIdMatching(unittest.TestCase):
    def test_notification_and_stale_responses_are_skipped(self):
        """无 id 的 notification 与错位旧响应被跳过，直到匹配本请求 id。"""
        svc = _service([
            json.dumps({"jsonrpc": "2.0", "method": "log", "params": {}}),          # notification（无 id）
            json.dumps({"jsonrpc": "2.0", "id": 99, "result": {"stale": True}}),    # 错位旧响应
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}),        # 本请求响应
        ])
        resp = svc._send_request("tools/list")
        self.assertEqual(resp["id"], 1)
        self.assertTrue(resp["result"]["ok"])

    def test_non_json_lines_skipped(self):
        svc = _service(["not json", json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})])
        resp = svc._send_request("tools/list")
        self.assertEqual(resp.get("id"), 1)

    def test_timeout_returns_error(self):
        svc = _service([])  # 无更多行 → readline 返回空 → 无响应
        resp = svc._send_request("tools/list")
        self.assertIn("error", resp)


class TestListToolsCaching(unittest.TestCase):
    def test_error_response_not_cached(self):
        """启动抖动返回 error → 返回空列表且不写缓存，下次调用可重试。"""
        svc = _service([json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}})])
        self.assertEqual(svc.list_tools(), [])
        self.assertIsNone(svc._tools_cache, "错误响应不应写入缓存")
        # 第二次成功 → 正常缓存
        svc._process = _FakeProc([json.dumps({"jsonrpc": "2.0", "id": 2,
                                              "result": {"tools": [{"name": "t1"}]}})])
        tools = svc.list_tools()
        self.assertEqual(tools, [{"name": "t1"}])
        self.assertIsNotNone(svc._tools_cache)

    def test_success_response_cached(self):
        svc = _service([json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "result": {"tools": [{"name": "t1"}]}})])
        svc.list_tools()
        svc._process = _FakeProc([])  # 再读也读不到 → 若未缓存会返回 []
        self.assertEqual(svc.list_tools(), [{"name": "t1"}])


class TestCallTool(unittest.TestCase):
    def test_text_content_joined(self):
        svc = _service([json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
            {"type": "text", "text": "part1"}, {"type": "text", "text": "part2"},
        ]}})])
        out = svc.call_tool("t1", {})
        self.assertEqual(out, "part1\npart2")

    def test_error_returned_as_message(self):
        svc = _service([json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "error": {"code": -1, "message": "tool broke"}})])
        out = svc.call_tool("t1", {})
        self.assertIn("tool broke", out)


if __name__ == "__main__":
    unittest.main()
