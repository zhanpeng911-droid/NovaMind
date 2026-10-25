"""A1/A3 桌面 Shell 单元测试（不创建真实窗口）。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
import pytest

from novamind.webui import server as webui_server

_desktop = pytest.importorskip("novamind.desktop.shell", reason="native shell requires Windows")
DesktopBridge, DesktopSettings, DesktopShell = _desktop.DesktopBridge, _desktop.DesktopSettings, _desktop.DesktopShell


class _WakeHandler(BaseHTTPRequestHandler):
    token = None
    calls = 0

    def do_POST(self):  # noqa: N802
        if self.path == "/shell/wake":
            type(self).calls += 1
            type(self).token = self.headers.get("X-NovaMind-Token")
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        return


class TestDesktopSettings(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            original = DesktopSettings(
                bubble_visible=False, bubble_x=12, bubble_y=34,
                last_thread_id="thread-1",
            )
            original.save(path)
            loaded = DesktopSettings.load(path)
            self.assertEqual(loaded, original)

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertTrue(DesktopSettings.load(path).bubble_visible)


class TestWakeExisting(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _WakeHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _WakeHandler.calls = 0
        _WakeHandler.token = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.singleton_path = Path(self.tmp.name) / "singleton.json"
        self.patches = [
            mock.patch("novamind.core.config.WORKSPACE_DIR", self.tmp.name),
            mock.patch("novamind.desktop.shell._SINGLETON_PATH", self.singleton_path),
            mock.patch("novamind.desktop.shell._SETTINGS_PATH",
                       Path(self.tmp.name) / "settings.json"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.shell = DesktopShell(port=self.port)

    def test_wake_existing_posts_token(self):
        self.singleton_path.write_text(json.dumps({
            "pid": 1,
            "url": f"http://127.0.0.1:{self.port}",
            "token": "secret-token",
        }), encoding="utf-8")
        self.assertTrue(self.shell._wake_existing())
        self.assertEqual(_WakeHandler.calls, 1)
        self.assertEqual(_WakeHandler.token, "secret-token")

    def test_wake_existing_without_file(self):
        self.assertFalse(self.shell._wake_existing(timeout=0.01))

    def test_consume_selection(self):
        self.shell._selection = "selected text"
        self.assertEqual(self.shell.consume_selection(), "selected text")
        self.assertEqual(self.shell.consume_selection(), "")

    def test_toggle_bubble(self):
        class _Bubble:
            def __init__(self):
                self.shown = 0
                self.hidden = 0
                self.width = self.height = 64
                self.x = self.y = 100
                self.native = None

            def move(self, x, y):
                self.x, self.y = x, y

            def resize(self, width, height):
                self.width, self.height = width, height

            def restore(self):
                pass

            def show(self):
                self.shown += 1

            def hide(self):
                self.hidden += 1

        bubble = _Bubble()
        self.shell._bubble = bubble
        self.assertFalse(self.shell.toggle_bubble())
        self.assertEqual(bubble.hidden, 1)
        self.assertTrue(self.shell.toggle_bubble())
        self.assertEqual(bubble.shown, 1)


class TestDesktopBridge(unittest.TestCase):
    def test_bridge_delegates(self):
        class _FakeShell:
            def __init__(self):
                self.calls = []

            def show_panel(self):
                self.calls.append("show")

            def hide_panel(self):
                self.calls.append("hide")

            def show_menu(self):
                self.calls.append("menu")

            def hide_menu(self):
                self.calls.append("menu_hide")

            def choose_action(self, action):
                self.calls.append(("choose", action))

            def open_workspace(self, url=None, thread_id=None):
                self.calls.append(("open", url, thread_id))

            def toggle_bubble(self):
                return True

            def consume_selection(self):
                return "sel"

            def quit(self):
                self.calls.append("quit")

        shell = _FakeShell()
        bridge = DesktopBridge(shell)
        bridge.show_panel()
        bridge.hide_panel()
        bridge.show_menu()
        bridge.hide_menu()
        bridge.choose_action("translate")
        bridge.open_workspace("/", "t1")
        self.assertEqual(bridge.toggle_bubble(), True)
        self.assertEqual(bridge.get_selection(), "sel")
        bridge.quit()
        self.assertEqual(
            shell.calls,
            ["show", "hide", "menu", "menu_hide", ("choose", "translate"),
             ("open", "/", "t1"), "quit"],
        )


class TestShellWakeEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(webui_server.app)
        webui_server._wake_handler = None
        self.addCleanup(setattr, webui_server, "_wake_handler", None)

    def test_wake_without_handler_503(self):
        resp = self.client.post("/shell/wake")
        self.assertEqual(resp.status_code, 503)

    def test_wake_calls_registered_handler(self):
        calls = []
        webui_server.register_wake_handler(lambda: calls.append(1))
        resp = self.client.post("/shell/wake")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
