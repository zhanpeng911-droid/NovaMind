"""Second-launch startup races; no native windows or external services."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest

desktop = pytest.importorskip("novamind.desktop.shell", reason="native shell requires Windows")


class RetryClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []
        self.cancelled = False
        self.after_wait = lambda: None

    def monotonic(self):
        return self.now

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        self.after_wait()
        return self.cancelled


@pytest.fixture
def wake(monkeypatch, tmp_path):
    clock = RetryClock()
    shell = desktop.DesktopShell.__new__(desktop.DesktopShell)
    shell._stop = clock
    path = tmp_path / "singleton.json"
    monkeypatch.setattr(desktop, "_SINGLETON_PATH", path)
    monkeypatch.setattr(desktop, "time", SimpleNamespace(monotonic=clock.monotonic))
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.status = 200
    monkeypatch.setattr(desktop.urllib.request, "build_opener", MagicMock(return_value=opener))
    monkeypatch.setattr(desktop.urllib.request, "urlopen", opener.open)

    def write_info(token="startup-token", url="http://127.0.0.1:9876"):
        path.write_text(json.dumps({"pid": 123, "url": url, "token": token}), encoding="utf-8")

    return shell, clock, path, opener, write_info


def test_missing_info_is_reread_until_the_first_instance_is_ready(wake):
    shell, clock, _, opener, write_info = wake
    clock.after_wait = write_info
    assert shell._wake_existing()
    assert clock.waits
    assert opener.open.call_count == 1
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:9876/shell/wake"
    assert request.get_header("X-novamind-token") == "startup-token"
    assert request.method == "POST"


def test_connection_error_and_unready_endpoint_are_retried(wake):
    shell, clock, _, opener, write_info = wake
    write_info()
    response = opener.open.return_value
    opener.open.side_effect = [URLError("not yet listening"),
                              HTTPError("local", 503, "not ready", {}, None), response]
    assert shell._wake_existing()
    assert opener.open.call_count == 3
    assert len(clock.waits) == 2


def test_stale_token_is_reread_after_a_rejected_request(wake):
    shell, clock, _, opener, write_info = wake
    write_info(token="old-token")
    clock.after_wait = lambda: write_info(token="new-token")
    response = opener.open.return_value
    opener.open.side_effect = [HTTPError("local", 403, "rejected", {}, None), response]
    assert shell._wake_existing()
    tokens = [call.args[0].get_header("X-novamind-token") for call in opener.open.call_args_list]
    assert tokens == ["old-token", "new-token"]


@pytest.mark.parametrize("contents", [None, "{partial", "[]", '{}',
                                      '{"url":"http://127.0.0.1:9876","token":null}'])
def test_missing_or_invalid_info_stops_at_the_total_deadline(wake, contents):
    shell, clock, path, opener, _ = wake
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    assert not shell._wake_existing(timeout=0.5)
    assert clock.now == pytest.approx(0.5)
    opener.open.assert_not_called()
    assert path.exists() == (contents is not None)


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com", "file:///local",
                                  "http://user:pass@127.0.0.1:9876", "http://127.0.0.1:9876/other"])
def test_invalid_target_never_receives_the_local_token(wake, url):
    shell, _, _, opener, write_info = wake
    write_info(url=url)
    assert not shell._wake_existing(timeout=0.5)
    opener.open.assert_not_called()


def test_request_timeout_and_retry_wait_share_one_monotonic_deadline(wake):
    shell, clock, _, opener, write_info = wake
    write_info()
    timeouts = []

    def consume_timeout(request, *, timeout):
        timeouts.append(timeout)
        clock.now += timeout
        raise URLError("connection timed out")

    opener.open.side_effect = consume_timeout
    assert not shell._wake_existing(timeout=2.5)
    assert clock.now == pytest.approx(2.5)
    assert len(timeouts) >= 2
    assert 0 < timeouts[-1] < timeouts[0] <= 1
    assert sum(timeouts) + sum(clock.waits) == pytest.approx(2.5)


@pytest.mark.parametrize("cancel_before", [True, False])
def test_cancellation_stops_retrying(wake, cancel_before):
    shell, clock, _, opener, write_info = wake
    write_info()
    clock.cancelled = cancel_before
    clock.after_wait = lambda: setattr(clock, "cancelled", True)
    opener.open.side_effect = URLError("not ready")
    assert not shell._wake_existing()
    assert opener.open.call_count == (0 if cancel_before else 1)
    assert len(clock.waits) == (0 if cancel_before else 1)


def test_unexpected_programming_errors_are_not_swallowed(wake):
    shell, _, _, opener, write_info = wake
    write_info()
    opener.open.side_effect = RuntimeError("unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        shell._wake_existing()


@pytest.mark.parametrize("stage", ["read", "http"])
@pytest.mark.parametrize("cancel", [False, True])
def test_blocked_io_cannot_extend_the_deadline_or_restart_after_cancellation(
        wake, monkeypatch, stage, cancel):
    import time

    shell, _, path, opener, write_info = wake
    write_info()
    contents = path.read_text(encoding="utf-8")
    shell._stop = threading.Event()
    monkeypatch.setattr(desktop, "time", time)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    retry = shell._retry_wake_until

    def tracked_retry(*args):
        try:
            return retry(*args)
        finally:
            finished.set()

    def block(*args, **kwargs):
        entered.set()
        if cancel:
            shell._stop.set()
        assert release.wait(2), "test must release its blocked IO worker"
        return contents if stage == "read" else opener.open.return_value

    monkeypatch.setattr(shell, "_retry_wake_until", tracked_retry)
    if stage == "read":
        monkeypatch.setattr(desktop, "_SINGLETON_PATH", SimpleNamespace(read_text=block))
    else:
        opener.open.side_effect = block
    started = time.monotonic()
    try:
        assert not shell._wake_existing(timeout=0.15)
        assert entered.is_set()
        assert time.monotonic() - started < 0.8
        assert shell._stop.is_set() == cancel
    finally:
        release.set()
        assert finished.wait(1)
    assert opener.open.call_count == (0 if stage == "read" else 1)


@pytest.mark.parametrize("success,cancelled,exit_code,prompt_count",
                         [(True, False, 0, 0), (False, False, 2, 1), (False, True, 2, 0)])
def test_second_launch_only_wakes_or_shows_a_safe_failure(monkeypatch, success, cancelled,
                                                         exit_code, prompt_count):
    shell = desktop.DesktopShell.__new__(desktop.DesktopShell)
    shell._instance = MagicMock()
    shell._instance.acquire.return_value = False
    shell._stop = threading.Event()
    if cancelled:
        shell._stop.set()
    shell._wake_existing = MagicMock(return_value=success)
    shell._create_windows = MagicMock()
    shell._cleanup = MagicMock()
    backend = MagicMock()
    prompt = MagicMock()
    monkeypatch.setattr(desktop, "_find_available_url", backend)
    monkeypatch.setattr(desktop, "_show_wake_failure", prompt, raising=False)
    assert shell.run() == exit_code
    assert prompt.call_count == prompt_count
    shell._instance.acquire.assert_called_once_with()
    backend.assert_not_called()
    shell._create_windows.assert_not_called()
    shell._cleanup.assert_not_called()


def test_native_failure_prompt_has_only_static_safe_text(monkeypatch):
    import ctypes

    message_box = MagicMock(return_value=1)
    monkeypatch.setattr(ctypes, "WinDLL", MagicMock(return_value=SimpleNamespace(MessageBoxW=message_box)))
    desktop._show_wake_failure()
    args = message_box.call_args.args
    assert args[0] is None
    assert "NovaMind" in args[1]
    assert "重试" in args[1]
    assert "系统托盘" in args[1]
    assert "token" not in args[1].lower()
    assert "singleton" not in args[1].lower()
    assert args[2] == "NovaMind"


@pytest.mark.parametrize("ready", [True, False])
def test_wake_handler_is_registered_only_after_native_readiness(monkeypatch, ready):
    from fastapi.testclient import TestClient

    from novamind.desktop import branding
    from novamind.webui import local_guard, server

    shell = desktop.DesktopShell.__new__(desktop.DesktopShell)
    shell._desktop_ready = threading.Event()
    shell._stop = threading.Event()
    for name in ("bubble", "panel", "menu", "workspace"):
        setattr(shell, "_" + name, SimpleNamespace(events=SimpleNamespace(
            shown=SimpleNamespace(wait=MagicMock(return_value=ready)))))
    shell.open_workspace = MagicMock()
    shell._apply_desktop_settings = MagicMock()
    monkeypatch.setattr(server, "_wake_handler", None)
    monkeypatch.setattr(local_guard, "_ENABLED", False)
    register = MagicMock(wraps=server.register_wake_handler)
    monkeypatch.setattr(server, "register_wake_handler", register)
    monkeypatch.setattr(branding, "apply_window_theme", MagicMock())
    monkeypatch.setattr(branding, "resolve_theme", lambda theme: theme)
    monkeypatch.setattr(desktop, "settings", lambda: {"appearance": {"theme": "light"},
                                                    "desktop": {"open_workspace": False}})
    client = TestClient(server.app)
    assert client.post("/shell/wake").status_code == 503
    shell._after_start()
    assert shell._desktop_ready.is_set() == ready
    assert register.call_count == int(ready)
    if ready:
        register.assert_called_once_with(shell.open_workspace)
    shell.open_workspace.assert_not_called()
    assert client.post("/shell/wake").status_code == (200 if ready else 503)
    assert shell.open_workspace.call_count == int(ready)


def test_primary_startup_keeps_wake_unavailable_until_the_native_callback(monkeypatch):
    from novamind.core import personal_upgrade
    from novamind.webui import server

    shell = desktop.DesktopShell.__new__(desktop.DesktopShell)
    shell._instance = MagicMock()
    shell._instance.acquire.return_value = True
    shell._settings = SimpleNamespace(bubble_visible=True)
    shell._host, shell._port = "127.0.0.1", 8765
    for name in ("_write_singleton_info", "_create_windows", "_watch", "_after_start",
                 "_on_hotkey", "open_workspace", "quit", "toggle_bubble", "_open_reminder", "_cleanup"):
        setattr(shell, name, MagicMock())
    monkeypatch.setattr(server, "_wake_handler", None)
    monkeypatch.setattr(personal_upgrade, "ensure_snapshot", MagicMock())
    monkeypatch.setattr(desktop, "settings", lambda: {"desktop": {"startup_bubble": False}})
    monkeypatch.setattr(desktop, "update_settings", MagicMock())
    monkeypatch.setattr(desktop, "_find_available_url", MagicMock(return_value=(MagicMock(), "http://127.0.0.1:9876")))
    monkeypatch.setattr(desktop, "Win32Shell", MagicMock())
    monkeypatch.setattr(desktop, "threading", SimpleNamespace(Thread=MagicMock()))

    def start(callback, **kwargs):
        assert callback is shell._after_start
        assert server._wake_handler is None

    monkeypatch.setattr(desktop.webview, "start", start)
    assert shell.run() == 0
    assert not shell._settings.bubble_visible
    shell.open_workspace.assert_not_called()
    shell._cleanup.assert_called_once_with()

