"""WebUI 后端测试（SSE 流式 + 内容归一化 + 诊断/监控/技能接口）。"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage

from novamind.core.state_machine import ConversationStore
from novamind.webui.live import LiveConversations
from novamind.webui.server import (
    ChatRequest,
    _content_str,
    _safe_id,
    _sse,
    _stream_chat,
    delete_session,
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


class _BlockingPersistingAgent:
    """模拟流式收尾才落盘、且缓存会话状态的运行中 Agent。

    Phase 5：astream 与 aclear_conversation 同锁互斥——模拟真实 Agent
    per-thread 协调器的契约（删除等待在飞轮次结束）。"""

    def __init__(self, store):
        self.store = store
        self._states = {"t1": object()}
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.turn_lock = asyncio.Lock()

    async def astream(self, _message, thread_id=None):
        async with self.turn_lock:
            self.started.set()
            await self.release.wait()
            self.store.save_message(thread_id, AIMessage(content="late response"))
            yield {"agent": {"messages": [AIMessage(content="late response")]}}

    def clear_conversation(self, thread_id):
        self._states.pop(thread_id, None)
        self.store.clear_thread(thread_id)

    async def aclear_conversation(self, thread_id):
        """Phase 3 删除契约：与 run/astream 按 thread 互斥的异步清除。"""
        async with self.turn_lock:
            self.clear_conversation(thread_id)


class _FakeRuntime:
    """Phase 5：WebRuntime 测试替身（capacity 用真信号量，组件可注入）。"""

    def __init__(self, agent, store):
        self._agent = agent
        self._store = store
        self.capacity = asyncio.Semaphore(1)
        self.live = LiveConversations()
        self.active_tasks = set()
        self.learning_revocations = []

    def agent_if_ready(self):
        return self._agent

    def get_history_store(self):
        return self._store

    def get_learning(self):
        from types import SimpleNamespace

        def revoke_thread(thread_id):
            assert self._store.load_messages(thread_id) == [], "learning cleanup must follow durable deletion"
            self.learning_revocations.append(thread_id)

        return SimpleNamespace(store=SimpleNamespace(revoke_thread=revoke_thread))

    def register_task(self, task):
        self.active_tasks.add(task)

    def unregister_task(self, task):
        self.active_tasks.discard(task)


class TestSessionDeletionConsistency(unittest.TestCase):
    async def _exercise(self, agent, *, timeout=5):
        async def consume_stream():
            async for _ in _stream_chat(ChatRequest(message="continue", thread_id="t1")):
                pass

        # Propagate child failures to the startup wait; cancel and join every
        # child before leaving the scenario, including on assertion/timeout.
        async with asyncio.timeout(timeout):
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(consume_stream())
                await agent.started.wait()
                delete_task = tasks.create_task(delete_session("t1"))
                await asyncio.sleep(0)
                self.assertFalse(delete_task.done(), "deletion must wait for the active turn")
                agent.release.set()
            return delete_task.result()

    def test_delete_waits_for_active_chat_then_clears_agent_cache_and_history(self):
        """删除与流式收尾竞态时，不能让旧会话重新写回 SQLite。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(db_path=os.path.join(tmp, "state.sqlite3"))
            self.addCleanup(store.close)
            store.save_message("t1", HumanMessage(content="existing message"))
            agent = _BlockingPersistingAgent(store)

            # Phase 5：组件持有者改为 WebRuntime；补丁 get_agent（流式）
            # 与 get_runtime（删除走 runtime.agent_if_ready）
            fake_runtime = _FakeRuntime(agent, store)
            with (
                mock.patch("novamind.webui.server.get_agent", return_value=agent),
                mock.patch("novamind.webui.server.get_runtime", return_value=fake_runtime),
            ):
                response = asyncio.run(self._exercise(agent)).model_dump(exclude_none=True)

            self.assertEqual(response, {"status": "ok"})
            self.assertNotIn("t1", agent._states)
            self.assertEqual(store.load_messages("t1"), [])
            self.assertEqual(fake_runtime.live.active, {})
            self.assertEqual(fake_runtime.live.tasks, {})
            self.assertEqual(fake_runtime.learning_revocations, ["t1"])
            self.assertEqual(fake_runtime.active_tasks, set())

    def test_startup_crash_is_propagated_instead_of_waiting_forever(self):
        agent = _BlockingPersistingAgent(None)
        closed = mock.Mock()

        async def crashing_stream(_request):
            try:
                raise RuntimeError("stream crashed before startup")
                yield  # pragma: no cover
            finally:
                closed()

        with mock.patch(__name__ + "._stream_chat", new=crashing_stream):
            with self.assertRaises(ExceptionGroup) as caught:
                asyncio.run(self._exercise(agent))
        self.assertEqual(len(caught.exception.exceptions), 1)
        error = caught.exception.exceptions[0]
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), "stream crashed before startup")
        closed.assert_called_once_with()
        self.assertFalse(agent.started.is_set())

    def test_startup_timeout_cancels_and_joins_stalled_stream(self):
        agent = _BlockingPersistingAgent(None)
        closed = mock.Mock()
        tasks = []

        async def stalled_stream(_request):
            tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
                yield  # pragma: no cover
            finally:
                closed()

        with mock.patch(__name__ + "._stream_chat", new=stalled_stream):
            with self.assertRaises(TimeoutError):
                asyncio.run(self._exercise(agent, timeout=0.1))
        closed.assert_called_once_with()
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].cancelled())

    def test_delete_timeout_cancels_and_joins_stalled_deletion(self):
        agent = _BlockingPersistingAgent(None)
        closed = mock.Mock()
        tasks = []

        async def stream(_request):
            agent.started.set()
            await agent.release.wait()
            yield "done"

        async def stalled_delete(_thread_id):
            tasks.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            finally:
                closed()

        with (
            mock.patch(__name__ + "._stream_chat", new=stream),
            mock.patch(__name__ + ".delete_session", new=stalled_delete),
        ):
            with self.assertRaises(TimeoutError):
                asyncio.run(self._exercise(agent, timeout=0.1))
        closed.assert_called_once_with()
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].cancelled())


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
    def test_application_info_uses_project_version_and_active_workspace(self):
        from pathlib import Path
        import tomllib

        from novamind.core import config
        from novamind.webui.server import app
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            config, "WORKSPACE_DIR", tmp
        ), mock.patch("novamind.core.doctor.run_doctor") as run:
            run.return_value.as_dict.return_value = {
                "ok": True, "counts": {}, "findings": [],
            }
            response = TestClient(app).get("/doctor")
        project = Path(__file__).resolve().parents[2] / "pyproject.toml"
        version = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["application"], {
            "version": version,
            "build_time": None,
            "mode": "source",
            "workspace": str(Path(tmp).resolve()),
        })

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
        self.assertIn("application", result)
        self.assertTrue(result["application"]["workspace"])


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
        result = asyncio.run(monitor_sessions()).model_dump()
        ids = {s["thread_id"] for s in result["sessions"]}
        self.assertEqual(ids, {"s1", "s2"})

    def test_monitor_events_parses(self):
        path = os.path.join(self.log_dir, "s1.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "tool_call", "ts": "2026-01-01T00:00:00", "tool": "search"}) + "\n")
            f.write(json.dumps({"event": "ai_message", "ts": "2026-01-01T00:00:01", "content": "hi"}) + "\n")
            f.write("{broken json\n")  # 真正的坏行
        result = asyncio.run(monitor_events("s1")).model_dump()
        self.assertEqual(len(result["events"]), 2)  # 坏行被跳过
        self.assertEqual(result["events"][0]["event"], "tool_call")

    def test_monitor_events_missing(self):
        result = asyncio.run(monitor_events("nonexistent")).model_dump()
        self.assertEqual(result["events"], [])


class TestSkillsEndpoint(unittest.TestCase):
    def test_list_skills(self):
        rec = mock.MagicMock()
        rec.skill_id = "search-1"
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
            result = asyncio.run(list_skills()).model_dump()

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["skills"][0]["skill_id"], "search-1")
        self.assertEqual(result["skills"][0]["name"], "search")
        self.assertEqual(result["skills"][0]["effective_rate"], 0.6)

    def test_list_skills_handles_error(self):
        """存储故障抛 HTTPException(500)，文案稳定且不含异常文本。"""
        with mock.patch(
            "novamind.webui.server.get_skill_store", side_effect=RuntimeError("db locked")
        ):
            with self.assertRaises(Exception) as ctx:
                asyncio.run(list_skills())
        self.assertIn("技能库暂不可用", str(ctx.exception))
        self.assertNotIn("db locked", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
