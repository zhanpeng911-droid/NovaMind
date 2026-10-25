"""Model failures with images must not all be blamed on vision support."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest

from novamind.webui.live import LiveConversations
from novamind.webui.server import ChatRequest, _stream_chat


def collect_failure(error, tmp_path, monkeypatch, *, with_image=True):
    from novamind.core import config
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))

    class Agent:
        async def astream(self, *args, **kwargs):
            raise error
            yield  # pragma: no cover

    async def collect():
        runtime = SimpleNamespace(live=LiveConversations(), capacity=asyncio.Semaphore(1),
                                  register_task=Mock(), unregister_task=Mock())
        with patch("novamind.webui.server.get_runtime", return_value=runtime), \
                patch("novamind.webui.server.get_agent", return_value=Agent()), \
                patch("novamind.core.images.message_content", return_value="offline"):
            return [json.loads(frame[6:]) async for frame in _stream_chat(ChatRequest(
                message="离线图片问题", images=["a" * 32] if with_image else [],
            ))]

    frames = asyncio.run(collect())
    assert frames[-1]["type"] == "done"
    note = next(frame for frame in frames if frame["type"] == "error")
    assert note["request_id"]
    assert "test-secret" not in note["message"]
    return note


@pytest.mark.parametrize("status,word", [(401, "凭据"), (429, "频繁"), (503, "服务")])
def test_provider_http_failure_is_not_reported_as_missing_vision(tmp_path, monkeypatch, status, word):
    request = httpx.Request("POST", "https://example.invalid/test-secret")
    error = httpx.HTTPStatusError("test-secret", request=request,
                                  response=httpx.Response(status, request=request))
    note = collect_failure(error, tmp_path, monkeypatch)
    assert word in note["message"]
    assert "视觉" not in note["message"]


def test_image_turn_timeout_has_a_timeout_note(tmp_path, monkeypatch):
    note = collect_failure(httpx.ReadTimeout("test-secret"), tmp_path, monkeypatch)
    assert "超时" in note["message"]
    assert "视觉" not in note["message"]


def test_unknown_error_is_still_generic_and_does_not_guess_vision(tmp_path, monkeypatch):
    note = collect_failure(RuntimeError("test-secret"), tmp_path, monkeypatch)
    assert note["code"] == "internal_error"
    assert "视觉" not in note["message"]
