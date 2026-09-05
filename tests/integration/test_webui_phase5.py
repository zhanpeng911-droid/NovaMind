"""WebUI 加固 Phase 5 专项测试。

覆盖：
- 游标编码/严格校验（版本/kind/字段类型/越界）与 400 invalid_cursor；
- BodyLimitMiddleware：Content-Length 与实际字节双通道 413、/chat JSON-only 415；
- SSE 断连语义：提前关闭时底层 astream 被 aclose（半轮回滚），不发 done；
- _require_loopback_host：拒绝非 loopback 绑定；
- 分页端点：sessions 稳定翻页、monitor events tail-follow、skills 分页 count 语义。
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from novamind.core.state_machine import ConversationStore
from novamind.webui.api_models import (
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
from novamind.webui.app import _require_loopback_host
from novamind.webui.server import BodyLimitMiddleware, _stream_chat, app


class TestCursorCodec(unittest.TestCase):
    def test_roundtrip(self):
        raw = encode_cursor("sessions", {"last_id": 42, "thread_id": "t1"})
        data = decode_cursor(raw, "sessions", {"last_id": int, "thread_id": str})
        self.assertEqual(data, {"last_id": 42, "thread_id": "t1"})

    def test_rejects_wrong_kind_version_and_types(self):
        raw = encode_cursor("sessions", {"last_id": 1, "thread_id": "t"})
        with self.assertRaises(InvalidCursorError):
            decode_cursor(raw, "history", {"last_id": int, "thread_id": str})
        with self.assertRaises(InvalidCursorError):
            decode_cursor("bm9wZQ", "sessions", {"last_id": int, "thread_id": str})
        # bool 不是合法 int（严格类型校验）
        bad = encode_cursor("sessions", {"last_id": True, "thread_id": "t"})
        with self.assertRaises(InvalidCursorError):
            decode_cursor(bad, "sessions", {"last_id": int, "thread_id": str})
        # 非 base64
        with self.assertRaises(InvalidCursorError):
            decode_cursor("!!not-base64!!", "sessions",
                          {"last_id": int, "thread_id": str})
        # 空游标
        with self.assertRaises(InvalidCursorError):
            decode_cursor("", "sessions", {"last_id": int, "thread_id": str})


class TestBodyLimitMiddleware(unittest.TestCase):
    """直接以 ASGI 层调用中间件，避免 TestClient 头部改写干扰。"""

    def setUp(self):
        self.sent = []
        self.responses = []

        async def app_mock(scope, receive, send):
            await receive()  # 模拟 FastAPI 读取请求体（超限在此抛错）
            self.sent.append("app-reached")

        self.app_mock = app_mock

    def _run(self, scope, receive):
        mw = BodyLimitMiddleware(self.app_mock, limit=16)

        async def _capture_send(message):
            self.responses.append(message)

        async def _call():
            await mw(scope, receive, _capture_send)
        asyncio.run(_call())

        start = next((m for m in self.responses
                      if m["type"] == "http.response.start"), None)
        return self.sent, start

    def test_content_length_over_limit_rejected_before_app(self):
        scope = {
            "type": "http", "method": "POST", "path": "/chat",
            "headers": [(b"content-length", b"999999"),
                        (b"content-type", b"application/json")],
        }
        sent, start = self._run(scope, self._noop_receive)
        self.assertEqual(sent, [])                      # app 未被触达
        self.assertEqual(start["status"], 413)

    def test_actual_bytes_over_limit_rejected(self):
        scope = {
            "type": "http", "method": "POST", "path": "/chat",
            "headers": [(b"content-length", b"4"),
                        (b"content-type", b"application/json")],
        }

        async def receive():
            return {"type": "http.request", "body": b"x" * 64, "more_body": False}

        sent, start = self._run(scope, receive)
        self.assertEqual(sent, [])                      # 实际字节超限，app 未触达
        self.assertEqual(start["status"], 413)

    def test_chat_non_json_rejected_415(self):
        scope = {
            "type": "http", "method": "POST", "path": "/chat",
            "headers": [(b"content-length", b"2"),
                        (b"content-type", b"text/plain")],
        }
        sent, start = self._run(scope, self._noop_receive)
        self.assertEqual(sent, [])
        self.assertEqual(start["status"], 415)

    def test_small_json_body_passes_through(self):
        scope = {
            "type": "http", "method": "POST", "path": "/chat",
            "headers": [(b"content-length", b"2"),
                        (b"content-type", b"application/json")],
        }

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent, _ = self._run(scope, receive)
        self.assertEqual(sent, ["app-reached"])

    async def _noop_receive(self):
        return {"type": "http.request", "body": b"", "more_body": False}


class TestSseDisconnectSemantics(unittest.TestCase):
    def test_early_close_closes_underlying_stream_without_done(self):
        closed = {"flag": False}

        class FakeAgent:
            def astream(self, message, thread_id=None):
                async def _gen():
                    try:
                        yield {"agent": {"messages": [AIMessage(content="hello")]}}
                        await asyncio.sleep(60)  # 永不正常结束
                    finally:
                        closed["flag"] = True
                return _gen()

        async def _run():
            with patch("novamind.webui.server.get_agent", return_value=FakeAgent()):
                gen = _stream_chat(
                    type("Req", (), {"message": "hi", "thread_id": "t"})()
                )
                first = await gen.__anext__()   # thread 帧
                self.assertIn('"thread"', first)
                second = await gen.__anext__()  # text 帧
                self.assertIn('"text"', second)
                # 模拟客户端断连：提前关闭
                await gen.aclose()

        asyncio.run(_run())
        self.assertTrue(closed["flag"], "底层 astream 应被 aclose（触发半轮回滚）")

    def test_runtime_error_yields_error_then_done(self):
        class ExplodingAgent:
            def astream(self, message, thread_id=None):
                async def _gen():
                    yield {"agent": {"messages": []}}
                    raise RuntimeError("boom mid-stream")
                return _gen()

        async def _run():
            with patch("novamind.webui.server.get_agent",
                       return_value=ExplodingAgent()):
                frames = []
                async for frame in _stream_chat(
                    type("Req", (), {"message": "hi", "thread_id": "t"})()
                ):
                    frames.append(frame)
                return frames

        frames = asyncio.run(_run())
        types = [json.loads(f.removeprefix("data: ").strip())["type"]
                 for f in frames if f.strip()]
        self.assertIn("error", types)
        self.assertEqual(types[-1], "done")


class TestLoopbackGuard(unittest.TestCase):
    def test_allows_loopback_hosts(self):
        for host in ("127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]"):
            _require_loopback_host(host)  # 不抛即通过

    def test_rejects_non_loopback(self):
        for host in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.2",
                     "example.com", ""):
            with self.assertRaises(ValueError, msg=host):
                _require_loopback_host(host)

    def test_start_server_rejects_bad_host_before_binding(self):
        from novamind.webui.app import _start_server

        with self.assertRaises(ValueError):
            _start_server("0.0.0.0", 9999)


class TestPaginationEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="novamind_p5_")
        self.store = ConversationStore(
            db_path=os.path.join(self.tmp, "state.sqlite3")
        )
        self.addCleanup(self.store.close)
        # 三个会话、不同消息量（thread_id 逆序即 last_id 逆序）
        for tid, n in (("t_a", 3), ("t_b", 2), ("t_c", 1)):
            from langchain_core.messages import HumanMessage
            self.store.save_messages(tid, [
                HumanMessage(content=f"{tid}-{i}", id=f"{tid}-{i}")
                for i in range(n)
            ])
        self._store_patch = patch("novamind.webui.server.get_history_store",
                                  return_value=self.store)
        self._store_patch.start()
        self.addCleanup(self._store_patch.stop)
        self.client = TestClient(app)

    def test_sessions_page_walk_no_dup_no_gap(self):
        seen = []
        cursor = None
        while True:
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = self.client.get("/sessions", params=params).json()
            seen.extend(s["thread_id"] for s in body["sessions"])
            cursor = body["pagination"].get("next_cursor")
            if not body["pagination"]["has_more"]:
                break
        self.assertEqual(set(seen), {"t_a", "t_b", "t_c"})
        self.assertEqual(len(seen), len(set(seen)))

    def test_sessions_invalid_cursor_400(self):
        resp = self.client.get("/sessions", params={"limit": 2, "cursor": "garbage"})
        self.assertEqual(resp.status_code, 400)
        err = resp.json()["error"]
        self.assertEqual(set(err.keys()), {"code", "message", "request_id"})
        self.assertEqual(err["code"], "invalid_cursor")

    def test_history_pagination_roundtrip(self):
        body = self.client.get("/history/t_a", params={"limit": 2}).json()
        self.assertEqual(len(body["messages"]), 2)
        self.assertIsNotNone(body["pagination"]["next_cursor"])
        cursor = body["pagination"]["next_cursor"]
        body2 = self.client.get(
            "/history/t_a", params={"limit": 2, "cursor": cursor}
        ).json()
        # 页从最新往回走：时间序 = 第 2 页（更旧）在前
        all_content = ([m["content"] for m in body2["messages"]]
                       + [m["content"] for m in body["messages"]])
        self.assertEqual(all_content, ["t_a-0", "t_a-1", "t_a-2"])
        self.assertIsNone(body2["pagination"].get("next_cursor"))

    def test_monitor_events_tail_follow(self):
        log_dir = self.tmp
        log_path = os.path.join(log_dir, "s_tail.jsonl")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "e1"}) + "\n")
            f.write(json.dumps({"event": "e2"}) + "\n")
        with patch("novamind.webui.server.LOG_DIR", log_dir):
            page1 = self.client.get(
                "/monitor/events/s_tail", params={"limit": 1}
            ).json()
            self.assertEqual([e["event"] for e in page1["events"]], ["e2"])
            follow_cursor = page1["pagination"]["next_cursor"]
            # 追加新行后，用游标只取新增
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": "e3"}) + "\n")
            page2 = self.client.get(
                "/monitor/events/s_tail",
                params={"limit": 10, "cursor": follow_cursor},
            ).json()
            self.assertEqual([e["event"] for e in page2["events"]], ["e3"])

    def test_monitor_events_truncated_cursor_400(self):
        log_dir = self.tmp
        log_path = os.path.join(log_dir, "s_cut.jsonl")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "e1"}) + "\n")
        with patch("novamind.webui.server.LOG_DIR", log_dir):
            page = self.client.get(
                "/monitor/events/s_cut", params={"limit": 1}
            ).json()
            cursor = page["pagination"]["next_cursor"]
            # 截断文件
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("")
            resp = self.client.get(
                "/monitor/events/s_cut", params={"limit": 1, "cursor": cursor}
            )
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["error"]["code"], "invalid_cursor")

    def test_skills_pagination_count_semantics(self):
        def _mk_rec(i):
            rec = MagicMock()
            rec.name = f"skill{i}"
            rec.skill_id = f"sid{i}"
            rec.description = "d"
            rec.total_selections = 0
            rec.total_applied = 0
            rec.total_completions = 0
            rec.total_fallbacks = 0
            rec.effective_rate = 0.0
            rec.enabled = True
            rec.is_active = True
            return rec

        recs = [_mk_rec(i) for i in range(3)]
        store = MagicMock()
        store.count_active.return_value = 3
        store.list_active_page.side_effect = lambda limit, cursor: (
            ([recs[-1]] if cursor is None else recs[:2]),
            (recs[-1].name.lower(), recs[-1].skill_id) if cursor is None else None,
        )
        with patch("novamind.webui.server.get_skill_store", return_value=store):
            page1 = self.client.get("/skills", params={"limit": 1}).json()
            self.assertEqual(page1["count"], 3)  # count 始终为全量 active 数
            self.assertEqual(len(page1["skills"]), 1)
            self.assertIsNotNone(page1["pagination"]["next_cursor"])
            page2 = self.client.get("/skills", params={
                "limit": 5, "cursor": page1["pagination"]["next_cursor"],
            }).json()
            self.assertEqual(len(page2["skills"]), 2)
            self.assertIsNone(page2["pagination"].get("next_cursor"))




class TestOpenApiContractFixes(unittest.TestCase):
    """加固收尾回归：OpenAPI 修正——/chat 200 仅 SSE、/doctor 真 schema、
    集合路由声明统一错误模型。"""

    @classmethod
    def setUpClass(cls):
        cls.schema = app.openapi()

    def test_chat_200_only_event_stream(self):
        content = self.schema["paths"]["/chat"]["post"]["responses"]["200"]["content"]
        self.assertEqual(set(content.keys()), {"text/event-stream"})

    def test_doctor_has_real_schema(self):
        s200 = self.schema["paths"]["/doctor"]["get"]["responses"]["200"]["content"]
        schema = s200["application/json"]["schema"]
        ref = schema.get("$ref", "")
        self.assertIn("DoctorResponse", ref)
        model = self.schema["components"]["schemas"]["DoctorResponse"]
        self.assertEqual(set(model["properties"].keys()), {"ok", "counts", "findings"})

    def test_collection_routes_declare_error_model(self):
        for path in ("/sessions", "/history/{thread_id}", "/skills",
                     "/monitor/sessions", "/monitor/events/{thread_id}"):
            op = self.schema["paths"][path]["get"]
            self.assertIn("400", op["responses"], path)
            self.assertIn("ErrorResponse", str(op["responses"]["400"]), path)
            self.assertIn("ErrorResponse", str(op["responses"]["500"]), path)


class TestSseErrorFrameNoLeak(unittest.TestCase):
    def test_error_frame_is_stable_without_exception_text(self):
        class ExplodingAgent:
            def astream(self, message, thread_id=None):
                async def _gen():
                    yield {"agent": {"messages": [AIMessage(content="x")]}}
                    raise RuntimeError("secret provider sk-abc failed at C:/path")
                return _gen()

        async def _run():
            with patch("novamind.webui.server.get_agent",
                       return_value=ExplodingAgent()):
                frames = []
                async for frame in _stream_chat(
                    type("Req", (), {"message": "hi", "thread_id": "t"})()
                ):
                    frames.append(frame)
                return frames

        frames = asyncio.run(_run())
        payloads = [json.loads(f.removeprefix("data: ").strip())
                    for f in frames if f.strip()]
        err = next(p for p in payloads if p["type"] == "error")
        self.assertEqual(err["code"], "internal_error")
        self.assertEqual(set(err.keys()),
                         {"type", "code", "message", "request_id"})
        self.assertNotIn("secret", err["message"])
        self.assertNotIn("sk-abc", err["message"])


class TestFrontendHardeningStatic(unittest.TestCase):
    """前端加固的静态断言（无浏览器环境下的最小验收）。"""

    def test_events_fetch_is_bounded_and_follow_cursor(self):
        page = open("novamind/webui/static/index.html", encoding="utf-8").read()
        self.assertIn("'?limit=200'", page, "monitor events 默认请求必须有界")
        self.assertIn("pageState.monitorEvents.cursor", page, "必须使用分页游标")
        # EOF 也保留更新入口：has_more 显示"继续加载"，否则"检查更新"
        self.assertIn("继续加载", page, "has_more=true 时显示继续加载")
        self.assertIn("检查更新", page, "EOF 仍可检查更新")
        self.assertIn("monitorEventsFetching", page, "增量请求必须防重入")
        self.assertIn("monitorEventsToken", page, "必须用请求 token 丢弃旧响应")

    def test_terminal_note_persists_in_state(self):
        page = open("novamind/webui/static/index.html", encoding="utf-8").read()
        logic = open("novamind/webui/static/chat_logic.js", encoding="utf-8").read()
        # 终态经纯 helper 构建（note 字段），渲染回放
        self.assertIn("NovaMindChatLogic.buildFinalMessage(turn)", page,
                      "最终消息经 helper 构建")
        self.assertIn("if (m.note)", page, "渲染必须回放终态提示")
        self.assertIn("errorText", logic, "helper 维护 errorText")
        self.assertIn('id="stop"', page, "STOP 按钮必须存在")


class TestMonitorEventsBoundSemantics(unittest.TestCase):
    """收尾修复 Phase 3：默认 200、非法 limit 422、EOF 空页保留游标。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="novamind_p3_")
        self.log_path = os.path.join(self.tmp, "s_bound.jsonl")
        with open(self.log_path, "w", encoding="utf-8") as f:
            for i in range(3):
                f.write(json.dumps({"event": f"e{i}", "n": i}) + "\n")
        self.client = TestClient(app)

    def _get(self, **params):
        with patch("novamind.webui.server.LOG_DIR", self.tmp):
            return self.client.get("/monitor/events/s_bound", params=params)

    def test_default_limit_200_bounded(self):
        body = self._get().json()
        self.assertEqual(body["pagination"]["limit"], 200)
        self.assertEqual([e["event"] for e in body["events"]], ["e0", "e1", "e2"])
        # 无 limit 不再全量语义：默认页即尾页 200（本文件不足 200 → 全部）
        self.assertEqual(len(body["events"]), 3)

    def test_invalid_limit_422(self):
        for bad in ("0", "1001", "abc", "-5"):
            resp = self._get(limit=bad)
            self.assertEqual(resp.status_code, 422, f"limit={bad}")
            self.assertEqual(resp.json()["error"]["code"], "validation_error")

    def test_eof_empty_page_keeps_cursor(self):
        """读到 EOF 后空增量页：has_more=false 但 next_cursor 非空，
        追加后同一游标能读到新事件（前端据此显示"检查更新"）。"""
        page1 = self._get(limit=1).json()
        cursor = page1["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor, "EOF 页也必须返回游标")
        # 无新增 → 空页 + 同游标
        empty = self._get(limit=10, cursor=cursor).json()
        self.assertEqual(empty["events"], [])
        self.assertFalse(empty["pagination"]["has_more"])
        self.assertIsNotNone(empty["pagination"]["next_cursor"])
        # 追加后同游标读到新事件
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "e3", "n": 3}) + "\n")
        after = self._get(limit=10, cursor=cursor).json()
        self.assertEqual([e["event"] for e in after["events"]], ["e3"])

    def test_truncation_invalid_cursor_400(self):
        page1 = self._get(limit=1).json()
        cursor = page1["pagination"]["next_cursor"]
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("")
        resp = self._get(limit=1, cursor=cursor)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_cursor")


if __name__ == "__main__":
    unittest.main()
