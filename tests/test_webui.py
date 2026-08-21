"""WebUI 后端测试（SSE 流式 + 内容归一化 + 诊断/监控/技能接口）。"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage

from novamind.core.state_machine import ConversationStore
from novamind.webui.server import (
    ChatRequest,
    _content_str,
    _safe_id,
    _sse,
    _stream_chat,
    doctor,
    list_skills,
    monitor_events,
    monitor_sessions,
)


class TestContentStr(unittest.TestCase):
    def test_plain_str(self):
        self.assertEqual(_content_str("hello"), "hello")

    def test_list_of_text_blocks(self):
        self.assertEqual(
            _content_str([{"type": "text", "text": "hi"}, {"type": "text", "text": "!"}]),
            "hi!",
        )

    def test_tool_call_block(self):
        self.assertEqual(
            _content_str([{"type": "tool_call", "name": "search"}]),
            "[tool_call:search]",
        )

    def test_other_coerced(self):
        self.assertEqual(_content_str(123), "123")


class TestSse(unittest.TestCase):
    def test_frame_serialization(self):
        frame = _sse({"type": "done"})
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        payload = json.loads(frame[6:].strip())
        self.assertEqual(payload, {"type": "done"})

    def test_unicode_not_escaped(self):
        frame = _sse({"type": "text", "content": "你好"})
        self.assertIn("你好", frame)


def _run_stream(req):
    async def collect():
        frames = []
        async for frame in _stream_chat(req):
            frames.append(frame)
        return frames

    return asyncio.run(collect())


class TestStreamChat(unittest.TestCase):
    def _payloads(self, frames):
        return [json.loads(f[6:].strip()) for f in frames]

    def test_stream_text_and_done(self):
        agent = mock.MagicMock()

        async def fake_astream(msg, thread_id=None):
            yield {"agent": {"messages": [AIMessage(content="你好呀")]}}

        agent.astream = fake_astream
        with mock.patch("novamind.webui.server.get_agent", return_value=agent):
            payloads = self._payloads(_run_stream(ChatRequest(message="hi", thread_id="t1")))

        types = [p["type"] for p in payloads]
        self.assertIn("thread", types)
        self.assertIn("text", types)
        self.assertEqual(types[-1], "done")
        thread_payload = next(p for p in payloads if p["type"] == "thread")
        self.assertEqual(thread_payload["thread_id"], "t1")

    def test_stream_generates_thread_id_when_missing(self):
        agent = mock.MagicMock()

        async def fake_astream(msg, thread_id=None):
            yield {"agent": {"messages": [AIMessage(content="x")]}}

        agent.astream = fake_astream
        with mock.patch("novamind.webui.server.get_agent", return_value=agent):
            payloads = self._payloads(_run_stream(ChatRequest(message="hi", thread_id=None)))

        thread_payload = next(p for p in payloads if p["type"] == "thread")
        self.assertTrue(thread_payload["thread_id"].startswith("gui_"))

    def test_stream_tool_call(self):
        agent = mock.MagicMock()

        async def fake_astream(msg, thread_id=None):
            yield {
                "agent": {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[{"name": "get_current_time", "args": {}, "id": "tc1"}],
                        )
                    ]
                }
            }

        agent.astream = fake_astream
        with mock.patch("novamind.webui.server.get_agent", return_value=agent):
            payloads = self._payloads(_run_stream(ChatRequest(message="time", thread_id="t1")))

        tool_payloads = [p for p in payloads if p["type"] == "tool"]
        self.assertEqual(len(tool_payloads), 1)
        self.assertEqual(tool_payloads[0]["name"], "get_current_time")

    def test_stream_error_then_done(self):
        agent = mock.MagicMock()

        async def fake_astream(msg, thread_id=None):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        agent.astream = fake_astream
        with mock.patch("novamind.webui.server.get_agent", return_value=agent):
            payloads = self._payloads(_run_stream(ChatRequest(message="hi", thread_id="t1")))

        types = [p["type"] for p in payloads]
        self.assertIn("error", types)
        self.assertEqual(types[-1], "done")


class TestListThreads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ConversationStore(db_path=self.tmp.name + "/state.sqlite3")
        self.addCleanup(self.store.close)

    def test_list_threads_returns_titles(self):
        self.store.save_message("t1", HumanMessage(content="你好"))
        self.store.save_message("t1", AIMessage(content="你好呀"))
        self.store.save_message("t2", HumanMessage(content="第二个会话"))
        threads = self.store.list_threads()
        self.assertEqual(len(threads), 2)
        titles = {t["thread_id"]: t["title"] for t in threads}
        self.assertIn("你好", titles["t1"])
        self.assertIn("第二个会话", titles["t2"])
        # t1 有 2 条消息
        self.assertEqual(
            next(t["message_count"] for t in threads if t["thread_id"] == "t1"), 2
        )

    def test_list_threads_empty(self):
        self.assertEqual(self.store.list_threads(), [])


class TestSafeId(unittest.TestCase):
    def test_alnum_passthrough(self):
        self.assertEqual(_safe_id("session_20260820_161950"), "session_20260820_161950")

    def test_strips_special_chars(self):
        self.assertEqual(_safe_id("a/b\\c:d"), "abcd")

    def test_empty_default(self):
        self.assertEqual(_safe_id("!!!"), "default")


class TestDoctorEndpoint(unittest.TestCase):
    def test_doctor_returns_report(self):
        fake_report = mock.MagicMock()
        fake_report.as_dict.return_value = {
            "ok": True,
            "counts": {"error": 0, "warning": 0, "info": 1},
            "findings": [],
        }
        with mock.patch(
            "novamind.core.doctor.run_doctor", return_value=fake_report
        ):
            result = asyncio.run(doctor())
        self.assertTrue(result["ok"])
        self.assertEqual(result["counts"]["info"], 1)

    def test_doctor_handles_error(self):
        with mock.patch(
            "novamind.core.doctor.run_doctor", side_effect=RuntimeError("boom")
        ):
            result = asyncio.run(doctor())
        self.assertFalse(result["ok"])
        self.assertEqual(result["counts"]["error"], 1)
        self.assertEqual(result["findings"][0]["code"], "doctor_failed")


class TestMonitorEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = self.tmp.name
        self._patch = mock.patch("novamind.webui.server.LOG_DIR", self.log_dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write_log(self, fname, events):
        path = os.path.join(self.log_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_monitor_sessions_lists_logs(self):
        self._write_log("s1.jsonl", [{"event": "ai_message", "ts": "2026-01-01T00:00:00"}])
        self._write_log("s2.jsonl", [{"event": "tool_call", "ts": "2026-01-02T00:00:00"}])
        result = asyncio.run(monitor_sessions())
        ids = {s["thread_id"] for s in result["sessions"]}
        self.assertEqual(ids, {"s1", "s2"})

    def test_monitor_events_parses(self):
        path = os.path.join(self.log_dir, "s1.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "tool_call", "ts": "2026-01-01T00:00:00", "tool": "search"}) + "\n")
            f.write(json.dumps({"event": "ai_message", "ts": "2026-01-01T00:00:01", "content": "hi"}) + "\n")
            f.write("{broken json\n")  # 真正的坏行
        result = asyncio.run(monitor_events("s1"))
        self.assertEqual(len(result["events"]), 2)  # 坏行被跳过
        self.assertEqual(result["events"][0]["event"], "tool_call")

    def test_monitor_events_missing(self):
        result = asyncio.run(monitor_events("nonexistent"))
        self.assertEqual(result["events"], [])


class TestSkillsEndpoint(unittest.TestCase):
    def test_list_skills(self):
        rec = mock.MagicMock()
        rec.name = "search"
        rec.description = "Search the web"
        rec.total_selections = 10
        rec.total_applied = 8
        rec.total_completions = 6
        rec.total_fallbacks = 0
        rec.effective_rate = 0.6
        rec.enabled = True
        rec.is_active = True

        fake_store = mock.MagicMock()
        fake_store.list_active.return_value = [rec]
        with mock.patch(
            "novamind.webui.server.get_skill_store", return_value=fake_store
        ):
            result = asyncio.run(list_skills())

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["skills"][0]["name"], "search")
        self.assertEqual(result["skills"][0]["effective_rate"], 0.6)

    def test_list_skills_handles_error(self):
        with mock.patch(
            "novamind.webui.server.get_skill_store", side_effect=RuntimeError("db locked")
        ):
            result = asyncio.run(list_skills())
        self.assertEqual(result["count"], 0)
        self.assertIn("db locked", result["error"])


if __name__ == "__main__":
    unittest.main()
