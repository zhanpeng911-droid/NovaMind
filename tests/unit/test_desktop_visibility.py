"""Regression checks for native visibility, settings reconciliation and quick entry."""
from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from novamind.core.personal import DEFAULTS
from novamind.webui.server import app

DesktopShell = pytest.importorskip("novamind.desktop.shell", reason="native shell requires Windows").DesktopShell


class WindowDouble:
    """Only actual pywebview Window methods; move can reveal a WinForms window."""

    def __init__(self, width=420, height=520):
        self.x, self.y, self.width, self.height = 100, 100, width, height
        self.visible = False
        self.on_top = True
        self.native = None
        self.scripts = []
        self.calls = []
        shown = threading.Event()
        shown.set()
        self.events = SimpleNamespace(shown=shown)

    def show(self):
        self.calls.append("show")
        self.visible = True

    def hide(self):
        self.calls.append("hide")
        self.visible = False

    def restore(self):
        self.calls.append("restore")

    def move(self, x, y):
        self.x, self.y, self.visible = x, y, True

    def resize(self, width, height):
        self.width, self.height = width, height

    def evaluate_js(self, script):
        self.scripts.append(script)
        return True

    def load_url(self, url):
        self.calls.append(url)


@pytest.fixture
def desktop():
    saved = copy.deepcopy(DEFAULTS)

    def update(section, changes):
        saved[section].update(changes)
        return copy.deepcopy(saved)

    with patch("novamind.desktop.shell.settings", side_effect=lambda: copy.deepcopy(saved)), \
         patch("novamind.desktop.shell.update_settings", side_effect=update):
        shell = DesktopShell()
        shell._bubble = WindowDouble(64, 64)
        shell._panel = WindowDouble()
        shell._menu = WindowDouble(268, 300)
        shell._workspace = WindowDouble(1000, 720)
        shell._desktop_ready = threading.Event()
        shell._desktop_ready.set()
        with patch.object(shell, "_work_area", return_value=(0, 0, 1536, 864)), \
             patch.object(shell, "_motion_enabled", return_value=False):
            yield shell, saved


def test_panel_open_runs_context_hook_with_real_window_api(desktop):
    shell, _ = desktop
    shell.show_panel()
    assert shell._panel.visible
    assert shell._panel.scripts, "Showing the window must not swallow an invalid bring_to_front call"
    assert shell._panel_open


@pytest.mark.parametrize("entry", ["bubble", "panel"])
def test_open_never_reassigns_native_topmost_from_a_worker(desktop, entry):
    shell, saved = desktop

    def reject_cross_thread_assignment(window, value):
        raise AssertionError("TopMost must only be set by the native UI thread")

    with patch.object(WindowDouble, "on_top", property(lambda window: True,
                      reject_cross_thread_assignment), create=True):
        if entry == "panel":
            shell.show_panel()
            assert shell._panel.visible
        else:
            shell._apply_desktop_settings(saved["desktop"], force=True)
            assert shell._bubble.visible


def test_startup_waits_for_all_native_windows_before_reconciling(desktop):
    shell, _ = desktop
    ready = []
    for name in ("bubble", "panel", "menu", "workspace"):
        window = getattr(shell, "_" + name)
        window.events.shown = SimpleNamespace(wait=lambda timeout, name=name: ready.append(name) or True)

    def reconcile(*args, **kwargs):
        assert ready == ["bubble", "panel", "menu", "workspace"]

    with patch.object(shell, "_apply_desktop_settings", side_effect=reconcile), \
         patch.object(shell, "open_workspace"):
        shell._after_start()


def test_repeated_menu_open_is_idempotent_and_explicit_hide_closes_it(desktop):
    shell, _ = desktop
    with patch("novamind.desktop.selection.foreground_window", return_value=None):
        shell.show_menu()
        assert shell._menu_visible and shell._menu.visible
        shell.show_menu()
        assert shell._menu_visible and shell._menu.visible
        shell.hide_menu()
        assert not shell._menu_visible and not shell._menu.visible


def test_startup_hidden_preference_survives_positioning(desktop):
    shell, saved = desktop
    saved["desktop"].update(bubble_visible=False, startup_bubble=False, open_workspace=False)
    shell._after_start()
    assert not shell._bubble.visible, "WinForms move() can show the ball; reconcile visibility afterwards"


def test_first_settings_reconciliation_applies_visibility(desktop):
    shell, saved = desktop
    saved["desktop"]["bubble_visible"] = False
    shell._bubble.visible = True
    shell._stop = MagicMock()
    shell._stop.wait.side_effect = [False, True]
    with patch("novamind.core.heartbeat.collect_reminders", return_value=[]), \
         patch("novamind.core.images.clean_stale_drafts"):
        shell._watch()
    assert not shell._bubble.visible, "The first tick cannot just record a previous settings snapshot"


def test_partial_desktop_save_keeps_live_position(tmp_path, monkeypatch):
    from novamind.core import config, personal
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    personal.update_settings("desktop", {"bubble_x": 640, "bubble_y": 240, "bubble_visible": False})
    response = TestClient(app).put("/personal/settings/desktop", json={"opacity": .8})
    assert response.status_code == 200
    current = personal.settings()["desktop"]
    assert current["bubble_x"] == 640 and current["bubble_y"] == 240
    assert not current["bubble_visible"]
    assert current["opacity"] == .8


def test_blank_chat_never_initializes_model():
    with patch("novamind.webui.server.get_agent", side_effect=AssertionError("must not call a model")) as agent:
        response = TestClient(app).post("/chat", json={})
    assert response.status_code == 422
    agent.assert_not_called()


def test_workbench_panel_entry_preserves_thread_context(desktop):
    shell, _ = desktop
    shell._bridge.show_panel("thread-from-workbench")
    assert 'novaMindOpen("thread-from-workbench")' in shell._panel.scripts[-1]
    assert shell._requested_thread is None


def test_panel_context_waits_until_quick_page_function_exists(desktop):
    shell, _ = desktop
    with patch.object(shell._panel, "evaluate_js", return_value=False):
        shell._bridge.show_panel("pending-thread")
    assert shell._requested_thread == "pending-thread"
    shell._push_panel()
    assert 'novaMindOpen("pending-thread")' in shell._panel.scripts[-1]
    assert shell._requested_thread is None


def test_recover_bubble_restores_visibility_and_keeps_appearance(desktop):
    shell, saved = desktop
    saved["desktop"].update(bubble_x=-9000, bubble_y=-5000, bubble_visible=False, opacity=.75)
    shell._bridge.reset_bubble()
    assert shell._bubble.visible
    assert 0 <= shell._bubble.x < 1536 and 0 <= shell._bubble.y < 864
    assert saved["desktop"]["opacity"] == .75


def test_hidden_ball_stays_hidden_after_resize_and_page_reload(desktop):
    shell, saved = desktop
    saved["desktop"].update(bubble_visible=False, size=56)
    shell._apply_desktop_settings(saved["desktop"], force=True)
    shell._on_bubble_loaded()
    assert not shell._bubble.visible
    assert (shell._bubble.width, shell._bubble.height) == (72, 72)


def test_native_ball_size_is_not_clamped_to_webview_default_minimum(desktop):
    shell, _ = desktop
    with patch("novamind.desktop.shell.webview.create_window", return_value=MagicMock()) as create:
        shell._create_windows()
    bubble = create.call_args_list[0].kwargs
    assert bubble.get("min_size") == (56, 56)
    workspace = create.call_args_list[-1].kwargs
    assert workspace["js_api"] is shell._bridge


@pytest.mark.parametrize("dpi,expected", [(96, (1920, 1080)), (120, (1536, 864)), (144, (1280, 720))])
def test_monitor_bounds_use_the_same_logical_units_as_webview(dpi, expected):
    import ctypes
    from ctypes import wintypes
    shell = DesktopShell()
    shell._bubble = WindowDouble(64, 64)
    shell._bubble.native = SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 123))
    native = SimpleNamespace(MonitorFromPoint=MagicMock(return_value=1),
                             GetMonitorInfoW=MagicMock(), GetDpiForWindow=MagicMock(return_value=dpi))

    def fill_info(handle, pointer):
        pointer._obj.work = wintypes.RECT(0, 0, 1920, 1080)
        return 1

    native.GetMonitorInfoW.side_effect = fill_info
    with patch.object(ctypes.windll, "user32", native):
        assert shell._work_area(100, 100) == (0, 0, *expected)
