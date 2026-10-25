"""A2 Quick Actions 测试：提示词/限额/流式/超时/API/升级会话。"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from novamind.webui.quick_actions import (
    MAX_INPUT_CHARS,
    QuickActionError,
    QuickActionService,
    build_prompt,
)
from novamind.webui.server import app


class _Chunk:
    def __init__(self, content: str):
        self.content = content


class _FakeStreamLLM:
    def __init__(self, chunks: list[str]):
        self.chunks = chunks
        self.prompts: list[str] = []

    async def astream(self, prompt):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            yield _Chunk(chunk)


class _SlowLLM:
    async def astream(self, prompt):
        await asyncio.sleep(5)
        yield _Chunk("late")


class _FakeStore:
    def __init__(self):
        self.saved = []

    def save_message(self, thread_id, message):
        self.saved.append((thread_id, message))


class _FakeRuntime:
    closing = False

    def __init__(self, llm, store):
        from novamind.webui.live import LiveConversations

        self._llm = llm
        self._store = store
        self.capacity = asyncio.Semaphore(4)
        self.live = LiveConversations()

    def register_task(self, task):
        pass

    def unregister_task(self, task):
        pass

    async def get_agent(self):
        return self

    async def append_history(self, thread_id, messages):
        for message in messages:
            self._store.save_message(thread_id, message)

    async def get_quick_llm(self):
        return self._llm

    def get_history_store(self):
        return self._store


class TestQuickPrompt(unittest.TestCase):
    def test_prompt_contains_action_preferences_and_text(self):
        prompt = build_prompt(
            "translate", "hello world",
            target_language="中文",
            preferences=["偏好简洁", "偏好技术术语保留英文", "偏好第三条", "第四条"],
        )
        self.assertIn("只输出译文", prompt)
        self.assertIn("目标语言：中文", prompt)
        self.assertIn("hello world", prompt)
        self.assertIn("偏好简洁", prompt)
        self.assertNotIn("第四条", prompt)  # 最多 3 条

    def test_unknown_action_rejected(self):
        with self.assertRaises(QuickActionError):
            build_prompt("bogus", "x")


class TestQuickService(unittest.TestCase):
    def test_stream_yields_chunks(self):
        llm = _FakeStreamLLM(["你", "好"])
        service = QuickActionService(llm, preferences=lambda _: ["偏好中文"])

        async def collect():
            return [c async for c in service.stream("explain", "解释这段")]

        chunks = asyncio.run(collect())
        self.assertEqual(chunks, ["你", "好"])
        self.assertIn("偏好中文", llm.prompts[0])

    def test_empty_and_oversized_text_rejected(self):
        service = QuickActionService(_FakeStreamLLM([]))
        with self.assertRaises(QuickActionError):
            asyncio.run(self._collect(service, "  "))
        with self.assertRaises(QuickActionError):
            asyncio.run(self._collect(service, "x" * (MAX_INPUT_CHARS + 1)))

    def test_timeout(self):
        service = QuickActionService(_SlowLLM(), timeout=0.05)
        with self.assertRaises(QuickActionError) as ctx:
            asyncio.run(self._collect(service, "内容"))
        self.assertIn("超时", str(ctx.exception))

    @staticmethod
    async def _collect(service, text):
        return [c async for c in service.stream("explain", text)]


class TestQuickAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.store = _FakeStore()
        self.llm = _FakeStreamLLM(["译", "文"])
        self.runtime = _FakeRuntime(self.llm, self.store)
        self.patches = [
            mock.patch("novamind.webui.server.get_runtime", return_value=self.runtime),
            mock.patch("novamind.webui.server._quick_preferences", return_value=[]),
            mock.patch("novamind.webui.server._quick_audit"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_action_stream(self):
        resp = self.client.post(
            "/quick/actions",
            json={"action": "translate", "text": "hello", "target_language": "中文"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers["content-type"])
        events = [
            json.loads(line[len("data: "):])
            for line in resp.text.splitlines() if line.startswith("data: ")
        ]
        self.assertEqual(events[0], {"type": "text", "content": "译"})
        self.assertEqual(events[1], {"type": "text", "content": "文"})
        self.assertEqual(events[-1], {"type": "done"})

    def test_invalid_action_422(self):
        resp = self.client.post(
            "/quick/actions", json={"action": "bogus", "text": "hello"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_too_long_input_422(self):
        resp = self.client.post(
            "/quick/actions",
            json={"action": "summarize", "text": "x" * (MAX_INPUT_CHARS + 1)},
        )
        self.assertEqual(resp.status_code, 422)

    def test_escalate_persists_message(self):
        resp = self.client.post(
            "/quick/escalate", json={"text": "帮我继续分析", "session_id": "sess-1"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["thread_id"], "sess-1")
        self.assertIn("thread=sess-1", payload["url"])
        self.assertEqual(len(self.store.saved), 1)
        thread_id, message = self.store.saved[0]
        self.assertEqual(thread_id, "sess-1")
        self.assertEqual(message.content, "帮我继续分析")

    def test_open_and_selection_ack(self):
        self.assertEqual(self.client.post("/quick/open").status_code, 200)
        resp = self.client.post(
            "/quick/selection",
            json={"captured": True, "chars": 42, "source_app": "browser"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
