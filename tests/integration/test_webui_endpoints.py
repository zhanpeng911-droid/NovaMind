"""
WebUI 端点错误分支 + app.py 启动辅助单元测试（P1 覆盖率盲区补齐）。

覆盖关键不变量：
  - /chat 参数校验失败 → 422
  - /monitor/events：thread_id 安全化、缺失文件返空、坏 JSON 行跳过
  - /history：未知会话返空、tool 消息过滤、tool_calls 提取为标签
  - /skills store 异常 → error 字段而非 500
  - app._wait_for_server：就绪返回 / 超时抛 TimeoutError
"""
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from novamind.webui.app import _find_available_url, _start_server, _wait_for_server, run_gui
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


class TestStartServer(unittest.TestCase):
    def test_starts_uvicorn_in_daemon_thread_and_returns_server(self):
        """_start_server：构造 uvicorn.Server、后台线程启动、返回 server 句柄。"""
        fake_server = MagicMock()
        thread_cls = MagicMock()
        with patch("novamind.webui.app.uvicorn.Server", return_value=fake_server) as server_cls, \
                patch("novamind.webui.app.threading.Thread", thread_cls):
            server = _start_server("127.0.0.1", 8765)
        self.assertIs(server, fake_server)
        config = server_cls.call_args.args[0]
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8765)
        thread_cls.assert_called_once()
        self.assertIn(("daemon", True), thread_cls.call_args.kwargs.items())
        thread_cls.return_value.start.assert_called_once()


class _ServerStub:
    """最小 server 桩：should_exit 初始为 False，行为与真实 uvicorn.Server 一致。"""

    def __init__(self):
        self.should_exit = False


class TestFindAvailableUrl(unittest.TestCase):
    def _patch_start(self, servers, created=None):
        """按调用顺序返回预置 server 的 _start_server mock。"""
        it = iter(servers)

        def fake_start(host, port):
            srv = next(it)
            if created is not None:
                created.append(srv)
            return srv

        return patch("novamind.webui.app._start_server", side_effect=fake_start)

    def test_first_port_ok(self):
        server = _ServerStub()
        with self._patch_start([server]),                 patch("novamind.webui.app._wait_for_server") as wait:
            got_server, url = _find_available_url("127.0.0.1", 9000, max_tries=3)
        self.assertIs(got_server, server)
        self.assertEqual(url, "http://127.0.0.1:9000")
        wait.assert_called_once()
        self.assertFalse(server.should_exit)

    def test_port_conflict_falls_back_to_next(self):
        """首端口就绪探测超时 → should_exit 置位并顺延下一端口。"""
        first, second = _ServerStub(), _ServerStub()
        with self._patch_start([first, second]),                 patch("novamind.webui.app._wait_for_server",
                      side_effect=[TimeoutError("t"), None]):
            _, url = _find_available_url("127.0.0.1", 9000, max_tries=3)
        self.assertTrue(first.should_exit)  # 旧实例被要求退出，防僵尸进程
        self.assertFalse(second.should_exit)
        self.assertEqual(url, "http://127.0.0.1:9001")

    def test_all_tries_exhausted_raises_runtime_error(self):
        created: list[_ServerStub] = []
        dead = [_ServerStub() for _ in range(2)]
        with self._patch_start(dead, created),                 patch("novamind.webui.app._wait_for_server",
                      side_effect=TimeoutError("t")):
            with self.assertRaises(RuntimeError) as ctx:
                _find_available_url("127.0.0.1", 9000, max_tries=2)
        self.assertIn("9000~9001", str(ctx.exception))
        # 每个尝试过的实例都被要求退出
        self.assertTrue(all(s.should_exit for s in created))


class TestRunGui(unittest.TestCase):
    def test_normal_path_opens_window_and_stops_server_on_exit(self):
        server = _ServerStub()
        with patch("novamind.webui.app._find_available_url",
                   return_value=(server, "http://127.0.0.1:8765")),                 patch("novamind.webui.app.webview.create_window") as cw,                 patch("novamind.webui.app.webview.start"):
            run_gui()
        # url 是第二个位置参数（第一个是窗口标题）
        self.assertEqual(cw.call_args.args[1], "http://127.0.0.1:8765")
        self.assertTrue(server.should_exit)  # finally 保证收尾

    def test_headless_degrades_to_browser_until_interrupt(self):
        """webview 起不来（headless）→ 打开系统浏览器并驻留，Ctrl+C 退出。"""
        server = _ServerStub()
        with patch("novamind.webui.app._find_available_url",
                   return_value=(server, "http://127.0.0.1:8765")),                 patch("novamind.webui.app.webview.create_window",
                      side_effect=RuntimeError("no display")),                 patch("novamind.webui.app.webbrowser.open") as browser,                 patch("novamind.webui.app.time.sleep",
                      side_effect=KeyboardInterrupt):
            run_gui()
        browser.assert_called_once_with("http://127.0.0.1:8765")
        self.assertTrue(server.should_exit)


if __name__ == "__main__":
    unittest.main()


class TestApiSuccessContract(unittest.TestCase):
    """加固 Phase 0：冻结 REST 成功响应的顶层字段。

    Phase 4/5 的分页只允许"新增"字段（如 pagination）；改名/删除即违约。
    """

    def setUp(self):
        self.client = TestClient(app)

    def test_sessions_contract(self):
        rec = {"thread_id": "t", "title": "会话", "message_count": 1, "last_ts": "2026-01-01"}
        with patch("novamind.webui.server.get_history_store") as gh:
            gh.return_value.list_threads.return_value = [rec]
            body = self.client.get("/sessions").json()
        self.assertEqual(set(body.keys()), {"sessions"})
        self.assertEqual(body["sessions"][0]["thread_id"], "t")

    def test_history_contract(self):
        from langchain_core.messages import AIMessage
        with patch("novamind.webui.server.get_history_store") as gh:
            gh.return_value.load_messages.return_value = [AIMessage(content="ok")]
            body = self.client.get("/history/t1").json()
        self.assertEqual(set(body.keys()), {"messages"})

    def test_skills_contract(self):
        store = MagicMock()
        store.list_active.return_value = []
        with patch("novamind.webui.server.get_skill_store", return_value=store):
            body = self.client.get("/skills").json()
        self.assertEqual(set(body.keys()), {"skills", "count"})
        self.assertEqual(body["count"], 0)

    def test_monitor_events_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("novamind.webui.server.LOG_DIR", tmp):
                body = self.client.get("/monitor/events/none").json()
        self.assertEqual(set(body.keys()), {"events"})

    def test_health_contract(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})


class TestSseChatContract(unittest.TestCase):
    """冻结 /chat SSE 事件类型与顺序：thread → [tool|text]* → done。"""

    def setUp(self):
        self.client = TestClient(app)

    def test_chat_stream_event_types_and_fields(self):
        from langchain_core.messages import AIMessage

        class _FakeStreamAgent:
            async def astream(self, message, thread_id=None):
                yield {"agent": {"messages": [AIMessage(content="", tool_calls=[
                    {"name": "calculator", "args": {}, "id": "1"}])]}}
                yield {"agent": {"messages": [AIMessage(content="算好了")]}}

        frames = []
        with patch("novamind.webui.server.get_agent", return_value=_FakeStreamAgent()):
            with self.client.stream("POST", "/chat",
                                    json={"message": "算一下", "thread_id": "sse_t"}) as resp:
                self.assertEqual(resp.status_code, 200)
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[len("data: "):]))

        types = [f["type"] for f in frames]
        self.assertEqual(types[0], "thread")
        self.assertIn("thread_id", frames[0])          # thread 帧携带 thread_id（前端依赖）
        self.assertIn("tool", types)                     # 工具帧
        self.assertIn("text", types)                     # 文本帧
        self.assertEqual(types[-1], "done")              # done 恒为最后一帧
        tool_frame = next(f for f in frames if f["type"] == "tool")
        self.assertEqual(tool_frame["name"], "calculator")
        text_frame = next(f for f in frames if f["type"] == "text")
        self.assertIn("content", text_frame)


class TestOpenApiContract(unittest.TestCase):
    """冻结 OpenAPI 路由面（Phase 5 改 SSE 声明/响应模型时同步更新本类）。"""

    def test_all_known_routes_present(self):
        schema = app.openapi()
        expected = {
            "/chat", "/health", "/doctor",
            "/monitor/sessions", "/monitor/events/{thread_id}",
            "/skills", "/sessions", "/history/{thread_id}", "/sessions/{thread_id}",
        }
        self.assertTrue(expected.issubset(schema["paths"].keys()),
                        f"路由缺失: {expected - set(schema['paths'].keys())}")

    def test_chat_is_post_and_sessions_is_get(self):
        schema = app.openapi()
        self.assertIn("post", schema["paths"]["/chat"])
        self.assertIn("get", schema["paths"]["/sessions"])
        self.assertIn("delete", schema["paths"]["/sessions/{thread_id}"])


if __name__ == "__main__":
    unittest.main()
