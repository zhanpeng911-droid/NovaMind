"""NovaMind 桌面 Shell（A1/A3）。

组成：
- 单实例（命名互斥量）；二次启动通过 loopback `/shell/wake` 唤醒既有实例；
- pywebview 悬浮球 + 轻量 Quick Panel + 完整工作台窗口（复用现有 WebUI）；
- Win32 托盘 + 全局热键（默认 Ctrl+Shift+Space）；
- 热键打开通用对话；只有明确选择文本快捷动作才 best-effort 捕获选区；
- 退出时停止后端、移除单实例信息文件、释放互斥量。

工作台与 Quick Panel 通过同一 loopback 后端与 WebRuntime 通信。
"""

from __future__ import annotations

import json
import http.client
import os
import threading
import logging
import time
import urllib.request
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass
from pathlib import Path

import webview

from novamind.core.config import WORKSPACE_DIR
from novamind.webui.app import _find_available_url

from .selection import capture_selection
from .single_instance import SingleInstance
from .win32_shell import Win32Shell
from novamind.core.personal import settings, update_settings

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(WORKSPACE_DIR) / "desktop_settings.json"
_SINGLETON_PATH = Path(WORKSPACE_DIR) / "desktop_singleton.json"


class _WakeNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A local session token must never follow a redirect to another service.
        return None


def _show_wake_failure() -> None:
    """Visible feedback for the windowed second process, before any GUI starts."""
    import ctypes
    from ctypes import wintypes

    try:
        message_box = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
        message_box.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        message_box.restype = ctypes.c_int
        message_box(
            None,
            "NovaMind 已在运行或正在启动，但本次未能打开工作台。\n"
            "请稍等片刻后重试，或从系统托盘打开 NovaMind。",
            "NovaMind",
            0x00010030,  # MB_OK | MB_ICONWARNING | MB_SETFOREGROUND
        )
    except OSError:
        logger.error("could not display the existing-instance notice")


@dataclass
class DesktopSettings:
    bubble_visible: bool = True
    bubble_x: int | None = None
    bubble_y: int | None = None
    last_thread_id: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "DesktopSettings":
        if path is None:
            current = settings()["desktop"]
            return cls(bubble_visible=current["bubble_visible"], bubble_x=current["bubble_x"],
                       bubble_y=current["bubble_y"])
        target = path or _SETTINGS_PATH
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**allowed)
        except Exception:
            return cls()

    def save(self, path: Path | None = None) -> None:
        if path is None:
            update_settings("desktop", {"bubble_visible": self.bubble_visible,
                                        "bubble_x": self.bubble_x, "bubble_y": self.bubble_y})
            return
        target = path or _SETTINGS_PATH
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except OSError:
            pass


class DesktopBridge:
    """暴露给 Quick Panel / 悬浮球的 JS API。"""

    def __init__(self, shell: "DesktopShell") -> None:
        self._shell = shell

    def show_panel(self, thread_id: str | None = None):
        if thread_id:
            self._shell.show_panel(thread_id=thread_id)
        else:
            self._shell.show_panel()
        return True

    def reset_bubble(self):
        self._shell.reset_bubble()
        return True

    def get_state(self):
        from novamind.core.personal import desktop_status
        result = desktop_status.get("last_turn", {})
        if self._shell._panel_open:
            self._shell._seen_result = result.get("id")
        running = self._shell._has_active_task()
        return {**settings(), "hotkey_error": self._shell._hotkey_error,
                "running": running, "selection": bool(self._shell._selection),
                "result": result if result.get("id") != self._shell._seen_result else {},
                "state": ("dragging" if self._shell._drag_origin else "running" if running else
                          "panel_open" if self._shell._panel_open else "menu_open" if self._shell._menu_visible else "idle")}

    def drag_ball(self, dx: int, dy: int, finished: bool = False):
        self._shell.drag_ball(dx, dy, finished)
        return True

    def present_bubble_surface(self, data: str):
        from .bubble_surface import BubbleSurface
        window = self._shell._bubble
        if window is None or window.native is None:
            return False
        # Bridge calls run on workers. Only the surface's UI callback mutates
        # the native form; the existing WebView remains the renderer/bridge.
        surface = getattr(window, '_novamind_surface', None)
        if surface is None:
            surface = BubbleSurface(window, self._shell)
            window._novamind_surface = surface
        return surface.present(data)

    def set_workspace_theme(self, theme: str, background: str | None = None,
                            foreground: str | None = None):
        from .branding import apply_window_theme
        return apply_window_theme(self._shell._workspace, theme, background, foreground)

    def open_settings(self):
        self._shell.open_workspace(url="/?view=settings")
        return True

    def open_schedule(self):
        self._shell.open_workspace(url="/?view=schedule")
        return True

    def hide_panel(self):
        self._shell.hide_panel()
        return True

    def show_menu(self):
        self._shell.show_menu()
        return True

    def hide_menu(self):
        self._shell.hide_menu()
        return True

    def choose_action(self, action: str):
        self._shell.choose_action(action)
        return True

    def open_workspace(self, url: str | None = None, thread_id: str | None = None):
        self._shell.open_workspace(url=url, thread_id=thread_id)
        return True

    def toggle_bubble(self):
        return self._shell.toggle_bubble()

    def get_selection(self):
        return self._shell.consume_selection()

    def quit(self):
        self._shell.quit()
        return True


class DesktopShell:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._host = host
        self._port = port
        self._settings = DesktopSettings.load()
        self._instance = SingleInstance()
        self._server = None
        self._url = ""
        self._tray: Win32Shell | None = None
        self._bubble = None
        self._panel = None
        self._menu = None
        self._workspace = None
        self._bridge = DesktopBridge(self)
        self._selection = ""
        self._selection_lock = threading.Lock()
        self._quitting = False
        self._stop = threading.Event()
        self._hotkey_error = ""
        self._menu_visible = False
        self._drag_origin = None
        self._foreground = None
        self._watcher = None
        self._panel_placed = False
        self._panel_open = False
        self._seen_result = None
        self._last_reminder = None
        self._desktop_ready = threading.Event()
        self._window_lock = threading.RLock()
        self._applied_desktop = None
        self._bubble_page_ready = threading.Event()
        self._requested_thread = None

    # ── 入口 ─────────────────────────────────────────────────────────

    def run(self) -> int:
        if not self._instance.acquire():
            # This is the losing process's startup path, before webview's UI
            # loop. Waiting here cannot block the existing instance's UI.
            if self._wake_existing():
                return 0
            if not self._stop.is_set():
                logger.warning("existing NovaMind instance could not be opened")
                _show_wake_failure()
            return 2
        try:
            from novamind.core.personal_upgrade import ensure_snapshot
            ensure_snapshot()
            self._settings.bubble_visible = settings()["desktop"]["startup_bubble"]
            update_settings("desktop", {"bubble_visible": self._settings.bubble_visible})
            self._server, self._url = _find_available_url(
                self._host, self._port, local_guard=True,
            )
            self._write_singleton_info()
            self._create_windows()
            self._tray = Win32Shell(
                on_open=self._on_hotkey,
                on_workspace=self.open_workspace,
                on_exit=self.quit,
                tooltip="NovaMind",
                on_toggle=self.toggle_bubble,
                on_settings=lambda: self.open_workspace(url="/?view=settings"),
                on_notification=self._open_reminder,
            )
            self._tray.start()
            self._watcher = threading.Thread(target=self._watch, daemon=True, name="novamind-desktop-state")
            self._watcher.start()
            from .branding import ICON_PATH
            webview.start(self._after_start, debug=False, icon=str(ICON_PATH))
            return 0
        finally:
            self._cleanup()

    # ── 窗口 ─────────────────────────────────────────────────────────

    def _create_windows(self) -> None:
        from .branding import PALETTES, resolve_theme, set_app_identity
        set_app_identity()
        # 只允许 .pywebview-drag-region 元素拖动窗口，避免 textarea/按钮误触发拖拽
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
        bubble_kwargs = {
            "url": self._url + "/bubble.html",
            "width": settings()["desktop"]["size"] + 16,
            "height": settings()["desktop"]["size"] + 16,
            # pywebview otherwise enforces 200×100, displacing the visible ball
            # and leaving a large transparent hit area at high DPI.
            "min_size": (56, 56),
            "frameless": True,
            "on_top": True,
            "easy_drag": False,
            "resizable": False,
            "transparent": True,
            "shadow": False,
            "focus": False,
            # WinForms has its own transparent-window hide-on-navigation path.
            # Create hidden and explicitly reconcile after native/page readiness.
            "hidden": True,
            "js_api": self._bridge,
        }
        if self._settings.bubble_x is not None:
            bubble_kwargs["x"] = self._settings.bubble_x
        if self._settings.bubble_y is not None:
            bubble_kwargs["y"] = self._settings.bubble_y
        self._bubble = webview.create_window("NovaMind Bubble", **bubble_kwargs)
        self._bubble.events.moved += self._on_bubble_moved
        self._bubble.events.loaded += self._on_bubble_loaded

        self._panel = webview.create_window(
            "NovaMind Quick",
            url=self._url + "/quick.html",
            width=420,
            height=520,
            min_size=(360, 360),
            frameless=True,
            on_top=True,
            easy_drag=True,
            resizable=True,
            hidden=True,
            transparent=True,
            shadow=False,
            js_api=self._bridge,
        )
        self._panel.events.closing += self._on_panel_closing
        self._panel.events.loaded += self._on_panel_loaded
        self._panel.events.resized += self._clip_desktop_windows

        self._menu = webview.create_window(
            "NovaMind Menu",
            url=self._url + "/bubble_menu.html",
            width=268,
            height=300,
            frameless=True,
            on_top=True,
            easy_drag=True,
            resizable=False,
            hidden=True,
            transparent=True,
            shadow=False,
            js_api=self._bridge,
        )
        self._menu.events.closing += self._on_menu_closing
        self._menu.events.loaded += self._on_menu_loaded

        self._workspace = webview.create_window(
            "NovaMind",
            url=self._url + "/",
            width=1000,
            height=720,
            min_size=(640, 480),
            background_color=PALETTES[resolve_theme(settings()['appearance']['theme'])][0],
            hidden=True,
            js_api=self._bridge,
        )
        self._workspace.events.closing += self._on_workspace_closing


    def show_panel(self, thread_id: str | None = None) -> None:
        if self._panel is None:
            return
        try:
            if thread_id:
                self._requested_thread = str(thread_id)[:200]
            with self._window_lock:
                self.hide_menu()
                self._panel_open = True
                # Restore before positioning: restoring a minimized native form can
                # otherwise put it back at its stale/off-screen restore bounds.
                self._panel.restore()
                self._place_near_bubble(self._panel, self._panel.width, self._panel.height)
                self._ensure_on_screen(self._panel)
                # on_top=True is already applied during native creation. The
                # WinForms setter is not marshalled by pywebview; never repeat
                # it from a bridge/hotkey worker (it can deadlock the UI).
                self._panel.show()
                self._clip_desktop_windows()
                self._push_panel()
        except Exception:
            self._panel_open = False
            logger.exception("could not open assistant panel")
            raise

    def hide_panel(self) -> None:
        if self._panel is None:
            return
        try:
            with self._window_lock:
                self._panel_open = False
                self._panel.hide()
        except Exception:
            logger.exception("could not hide assistant panel")

    def show_menu(self) -> None:
        """右键悬浮球显示快捷菜单；普通单击直接打开助手。"""
        if self._menu is None:
            self.show_panel()
            return
        if self._menu_visible:
            self._menu.show()
            return
        try:
            from .selection import foreground_window
            self._foreground = foreground_window()
            self._menu_visible = True
            self._place_near_bubble(self._menu, 268, 300)
            self._menu.restore()
            self._ensure_on_screen(self._menu)
            self._menu.show()
            self._clip_desktop_windows()
        except Exception:
            self._menu_visible = False
            logger.exception("could not open bubble menu")
            raise

    def hide_menu(self) -> None:
        self._menu_visible = False
        if self._menu is None:
            return
        try:
            self._menu.hide()
        except Exception:
            pass

    def choose_action(self, action: str) -> None:
        """菜单选择动作：关闭菜单 → 打开面板并预选动作。"""
        self.hide_menu()
        if action in {"translate", "explain", "summarize"} and self._foreground:
            from .selection import activate_window
            if activate_window(self._foreground):
                with self._selection_lock:
                    self._selection = capture_selection()
        self.show_panel()
        try:
            import json as _json

            self._panel.evaluate_js(
                f"window.novaMindSelectAction && window.novaMindSelectAction({_json.dumps(action)})"
            )
        except Exception:
            pass

    def _place_near_bubble(self, window, width: int, height: int) -> None:
        """优先贴着悬浮球显示，并夹在屏幕范围内。"""
        if window is self._panel and self._panel_placed:
            x, y = int(window.x), int(window.y)
            from .geometry import work_area
            left, top, right, bottom = work_area(window, x, y)
            window.move(max(left, min(x, right-width)), max(top, min(y, bottom-height)))
            return
        bx, by = 0, 0
        if self._bubble is not None:
            try:
                bx, by = int(self._bubble.x), int(self._bubble.y)
            except Exception:
                pass
        if bx == 0 and by == 0 and self._settings.bubble_x is not None:
            bx = int(self._settings.bubble_x)
            by = int(self._settings.bubble_y or 0)
        from .geometry import window_scale, work_area
        # Each native window has its own logical coordinate system on mixed-DPI
        # monitors. Convert the anchor before passing coordinates to the panel.
        scale = window_scale(self._bubble) / window_scale(window)
        bx, by = round(bx * scale), round(by * scale)
        left, top, right, bottom = work_area(window, bx, by)
        x = bx - width - 12 if bx - width - 12 >= left else bx + 72
        x = max(left + 8, min(x, right - width - 8))
        y = max(top + 8, min(by, bottom - height - 8))
        try:
            window.move(int(x), int(y))
            if window is self._panel:
                self._panel_placed = True
        except Exception:
            logger.exception("could not position desktop window")

    @staticmethod
    def _ensure_on_screen(window) -> None:
        """Re-read DPI/coordinates after a move, including monitor disconnection."""
        from .geometry import clamp, work_area
        x, y, width, height = int(window.x), int(window.y), window.width, window.height
        area = work_area(window, x, y)
        available_width, available_height = area[2] - area[0], area[3] - area[1]
        if width > available_width or height > available_height:
            window.resize(min(width, available_width), min(height, available_height))
            width, height = window.width, window.height
        x, y = clamp(x, y, width, height, area)
        window.move(x, y)

    def open_workspace(self, url: str | None = None, thread_id: str | None = None) -> None:
        if self._workspace is None:
            return
        target = url or (
            self._url + (f"/?thread={thread_id}" if thread_id else "/")
        )
        if target.startswith("/"):
            target = self._url + target
        if not target.startswith(self._url + "/"):
            return
        try:
            if url or thread_id:
                self._workspace.load_url(target)
            self._workspace.restore()
            self._ensure_on_screen(self._workspace)
            self._workspace.show()
        except Exception:
            logger.exception("could not open workspace")
            raise
        if thread_id:
            self._settings.last_thread_id = thread_id

    def toggle_bubble(self) -> bool:
        if self._bubble is None:
            return False
        with self._window_lock:
            self._settings.bubble_visible = not settings()["desktop"]["bubble_visible"]
            update_settings("desktop", {"bubble_visible": self._settings.bubble_visible})
            self._apply_desktop_settings(settings()["desktop"], force=True)
            if not self._settings.bubble_visible:
                self.hide_menu()
        return self._settings.bubble_visible

    def reset_bubble(self) -> None:
        """Recover a ball stranded off-screen without resetting its appearance."""
        update_settings("desktop", {"bubble_x": None, "bubble_y": None, "bubble_visible": True})
        self._apply_desktop_settings(settings()["desktop"], force=True)

    # ── 热键 / 选中文本 ──────────────────────────────────────────────

    def _on_hotkey(self) -> None:
        self.show_panel()

    def consume_selection(self) -> str:
        with self._selection_lock:
            text, self._selection = self._selection, ""
        return text

    def _push_panel(self, *args) -> None:
        self._clip_desktop_windows()
        try:
            ready = self._panel.evaluate_js(
                "Boolean(window.novaMindOpen && (window.novaMindOpen(" + json.dumps(self._requested_thread) + "), true))")
            if ready:
                self._requested_thread = None
        except Exception:
            logger.debug("panel not ready yet")

    def _on_panel_loaded(self, *args) -> None:
        self._clip_desktop_windows()
        if self._panel_open:
            self._push_panel()
        else:
            self._panel.hide()

    def _on_menu_loaded(self, *args) -> None:
        self._clip_desktop_windows()
        if not self._menu_visible:
            self._menu.hide()

    def _work_area(self, x: int, y: int) -> tuple[int, int, int, int]:
        from .geometry import work_area
        return work_area(self._bubble, x, y)

    def drag_ball(self, dx: int, dy: int, finished: bool = False) -> None:
        if self._bubble is None:
            return
        if self._drag_origin is None:
            self._drag_origin = (self._bubble.x, self._bubble.y)
        x, y = self._drag_origin[0] + dx, self._drag_origin[1] + dy
        left, top, right, bottom = self._work_area(x, y)
        width = settings()["desktop"]["size"] + 16
        x, y = max(left, min(x, right-width)), max(top, min(y, bottom-width))
        if finished and settings()["desktop"]["snap"]:
            snapped = left + 12 if x < (left+right)/2 else right-width-12
            if self._motion_enabled():
                for frame in range(1, 16):
                    progress = 1 - (1-frame/15)**3
                    self._bubble.move(int(x+(snapped-x)*progress), int(y))
                    time.sleep(.012)
            x = snapped
        self._bubble.move(int(x), int(y))
        if finished:
            self._drag_origin = None
            self._settings.bubble_x, self._settings.bubble_y = int(x), int(y)
            update_settings("desktop", {"bubble_x": int(x), "bubble_y": int(y)})

    @staticmethod
    def _motion_enabled() -> bool:
        if not settings()["desktop"]["motion"]:
            return False
        import ctypes
        from ctypes import wintypes
        animation = wintypes.BOOL(True)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(animation), 0)
        return bool(animation.value)

    def _after_start(self) -> None:
        deadline = time.monotonic() + 15
        # Hidden forms also emit shown during native creation. Waiting only
        # for the ball races against creation of the remaining WinForms forms.
        for name in ("bubble", "panel", "menu", "workspace"):
            window = getattr(self, "_" + name)
            if window is None or not window.events.shown.wait(max(0, deadline - time.monotonic())):
                logger.error("%s native window did not initialize", name)
                return
        if self._stop.is_set():
            return
        self._desktop_ready.set()
        from novamind.webui.server import register_wake_handler

        # Until native forms are ready the backend returns 503; a second launch
        # can retry instead of receiving a false success before a window exists.
        register_wake_handler(self.open_workspace)
        from .branding import apply_window_theme, resolve_theme
        apply_window_theme(self._workspace, resolve_theme(settings()['appearance']['theme']))
        data = settings()["desktop"]
        self._apply_desktop_settings(data, force=True)
        if data["open_workspace"]:
            self.open_workspace()

    def _on_bubble_loaded(self, *args) -> None:
        self._bubble_page_ready.set()
        self._clip_desktop_windows()
        if self._desktop_ready.is_set() and not self._quitting:
            self._apply_desktop_settings(settings()["desktop"], force=True)

    def _apply_desktop_settings(self, current: dict, *, force: bool = False) -> None:
        from .geometry import clamp
        if self._bubble is None:
            return
        with self._window_lock:
            previous = self._applied_desktop
            moved = force or previous is None or current["size"] != previous["size"] or (
                current["bubble_x"] != previous["bubble_x"] or current["bubble_y"] != previous["bubble_y"])
            size = current["size"] + 16
            if (self._bubble.width, self._bubble.height) != (size, size):
                self._bubble.resize(size, size)
                moved = True
            if moved:
                x = current["bubble_x"] if current["bubble_x"] is not None else self._bubble.x
                y = current["bubble_y"] if current["bubble_y"] is not None else self._bubble.y
                area = self._work_area(x, y)
                if current["bubble_x"] is None:
                    x, y = area[2]-size-20, area[3]-size-20
                x, y = clamp(x, y, size, size, area)
                self._bubble.move(x, y)
            # move/resize may reveal a hidden WinForms window: visibility goes last.
            from .geometry import is_visible
            visibility_lost = current["bubble_visible"] and is_visible(self._bubble) is False
            if force or moved or visibility_lost or previous is None or current["bubble_visible"] != previous["bubble_visible"]:
                if current["bubble_visible"]:
                    self._bubble.restore()
                    self._ensure_on_screen(self._bubble)
                    # Keep the creation-time on_top=True; see show_panel().
                    self._bubble.show()
                else:
                    self._bubble.hide()
            self._settings.bubble_visible = current["bubble_visible"]
            self._applied_desktop = dict(current)
            if force or moved:
                self._clip_desktop_windows()

    def _clip_desktop_windows(self, *args) -> None:
        from .geometry import clip_window
        for window in (self._bubble, self._panel, self._menu):
            if window is None or window.native is None:
                continue
            if window is self._bubble and getattr(window, '_novamind_alpha_surface', False):
                continue
            try:
                clip_window(window, circle=window is self._bubble)
            except (AttributeError, OSError, TypeError):
                logger.warning("desktop window shape could not be applied", exc_info=True)

    def _has_active_task(self) -> bool:
        try:
            from novamind.webui.server import _runtime
            return bool(_runtime and _runtime.active_task_count)
        except Exception:
            return False

    def _watch(self) -> None:
        from novamind.core import schedules
        from novamind.core.heartbeat import collect_reminders
        from novamind.core.images import clean_stale_drafts
        from novamind.core.personal import desktop_status
        last_cleanup = 0.0
        while not self._stop.wait(2):
            try:
                if not self._desktop_ready.is_set():
                    continue
                current = settings()["desktop"]
                if time.monotonic() - last_cleanup > 3600:
                    clean_stale_drafts()
                    last_cleanup = time.monotonic()
                self._settings.bubble_visible = current["bubble_visible"]
                if self._tray:
                    self._tray.set_hotkey(current["hotkey"])
                    self._hotkey_error = self._tray.error or ("热键已占用，请更换快捷键" if not self._tray.hotkey_registered else "")
                    desktop_status["hotkey_error"] = self._hotkey_error
                self._apply_desktop_settings(current)
                due = collect_reminders()
                if due:
                    self._last_reminder = due[-1]["id"]
                    text = (f"有 {len(due)} 项安排需要查看，点击打开日程。" if len(due) > 3 else
                            "\n".join(("错过提醒 · " if t["delivery"]["missed"] else "") +
                                      t["description"] + " · " + t["delivery"]["time"] for t in due))
                    accepted = bool(self._tray and self._tray.notify("NovaMind · 日程提醒", text))
                    desktop_status["notification"] = ("已交给系统通知；专注模式可能隐藏通知，未读事项仍在日程页。" if accepted else
                                                        "系统通知不可用；提醒已保留在日程页，请查看未读事项。")
                    for task in due:
                        schedules.mark_notification(task["id"], task["delivery"]["time"], accepted)
            except Exception:
                logger.exception("desktop state update failed")

    def _open_reminder(self) -> None:
        from urllib.parse import quote
        suffix = "&task=" + quote(self._last_reminder) if self._last_reminder else ""
        self.open_workspace(url="/?view=schedule" + suffix)

    # ── 单实例 ───────────────────────────────────────────────────────

    def _write_singleton_info(self) -> None:
        from novamind.webui.local_guard import session_token

        _SINGLETON_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SINGLETON_PATH.write_text(json.dumps({
            "pid": os.getpid(),
            "url": self._url,
            "token": session_token(),
        }), encoding="utf-8")

    def _wake_existing(self, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        cancelled, completed = threading.Event(), threading.Event()
        result, errors = [], []

        def wake() -> None:
            try:
                result.append(self._retry_wake_until(deadline, cancelled))
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        # urllib's timeout limits individual socket operations. A slow response
        # (or file read) must not keep this windowless process alive indefinitely.
        # One daemon owns all attempts and never holds native/UI resources.
        threading.Thread(target=wake, daemon=True, name="novamind-existing-wake").start()
        try:
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if completed.wait(min(0.05, remaining)):
                    if errors:
                        raise errors[0]
                    return bool(result[0]) and time.monotonic() <= deadline and not self._stop.is_set()
            return False
        finally:
            # Keep shell._stop intact: a timeout still needs the failure notice.
            cancelled.set()

    def _retry_wake_until(self, deadline: float, cancelled: threading.Event) -> bool:
        # Local traffic must not inherit a system proxy or forward the token.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _WakeNoRedirect())
        while not cancelled.is_set() and not self._stop.is_set() and time.monotonic() < deadline:
            try:
                # The mutex precedes both backend startup and this file. Reread
                # on each attempt, including after stale-port/token failures.
                data = json.loads(_SINGLETON_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("invalid singleton info")
                url, token = data.get("url"), data.get("token")
                if not isinstance(url, str) or not isinstance(token, str) or not token:
                    raise ValueError("invalid singleton info")
                if (len(token) > 512 or not token.isascii()
                        or any(not (c.isalnum() or c in "-_") for c in token)):
                    raise ValueError("invalid local token")
                target = urlsplit(url)
                if (target.scheme != "http" or target.hostname not in {"127.0.0.1", "::1", "localhost"}
                        or target.username is not None or target.password is not None
                        or target.path not in {"", "/"} or target.query or target.fragment
                        or target.port is None):
                    raise ValueError("invalid local target")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or cancelled.is_set() or self._stop.is_set():
                    return False
                request = urllib.request.Request(
                    url.rstrip("/") + "/shell/wake", method="POST",
                    headers={"X-NovaMind-Token": token},
                )
                with opener.open(request, timeout=min(1.0, remaining)) as response:
                    if response.status == 200:
                        return (not cancelled.is_set() and not self._stop.is_set()
                                and time.monotonic() <= deadline)
            except (OSError, ValueError, http.client.HTTPException):
                # Expected startup/file/HTTP failures only. Do not log request
                # exceptions: their URL, headers or paths can contain secrets.
                pass
            remaining = deadline - time.monotonic()
            if (remaining <= 0 or cancelled.is_set()
                    or self._stop.wait(min(0.15, remaining))):
                return False
        return False

    # ── 关闭 ─────────────────────────────────────────────────────────

    def _on_panel_closing(self):
        if self._quitting:
            return True
        self.hide_panel()
        return False

    def _on_menu_closing(self):
        if self._quitting:
            return True
        self.hide_menu()
        return False

    def _on_workspace_closing(self):
        if self._quitting:
            return True
        # 关闭完整工作台不退出应用：隐藏即可
        try:
            self._workspace.hide()
        except Exception:
            pass
        return False

    def _on_bubble_moved(self, *args) -> None:
        if self._drag_origin is not None:
            return
        try:
            self._settings.bubble_x = int(self._bubble.x)
            self._settings.bubble_y = int(self._bubble.y)
            update_settings("desktop", {"bubble_x": self._settings.bubble_x, "bubble_y": self._settings.bubble_y})
        except Exception:
            pass

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        for window in (self._workspace, self._panel, self._menu, self._bubble):
            try:
                window.destroy()
            except Exception:
                pass

    def _cleanup(self) -> None:
        self._stop.set()
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        if self._server is not None:
            self._server.should_exit = True
            thread = getattr(self._server, "novamind_thread", None)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=15)
        from novamind.webui.server import register_wake_handler
        register_wake_handler(None)
        try:
            data = json.loads(_SINGLETON_PATH.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                _SINGLETON_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        self._instance.release()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NovaMind desktop shell")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    return DesktopShell(port=args.port).run()


if __name__ == "__main__":
    raise SystemExit(main())
