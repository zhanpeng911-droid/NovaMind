"""Windows 原生薄壳：系统托盘 + 全局快捷键（ctypes，零第三方依赖）。

A0 方案 B 的最小实现：
- 隐藏顶层窗口承载托盘回调与热键消息；
- Shell_NotifyIconW 注册托盘图标，右键菜单「打开 / 退出」；
- RegisterHotKey 注册全局快捷键（默认 Ctrl+Shift+Space）；
- 独立线程跑 Win32 消息循环，与 pywebview 的 GUI 循环互不干扰。

非 Windows 平台 import 即报错；调用方应先判断 sys.platform。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - 平台分支
    raise ImportError("win32_shell is Windows-only")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_SET_HOTKEY = WM_APP + 2
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD = 0
NIM_DELETE = 2
NIM_SETVERSION = 4
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
IDI_APPLICATION = 32512

MF_STRING = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.RegisterHotKey.restype = wintypes.BOOL
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
_user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
]
_user32.TrackPopupMenu.restype = wintypes.UINT
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.LoadIconW.restype = wintypes.HICON
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
_user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.LoadImageW.restype = wintypes.HANDLE
_user32.DestroyIcon.argtypes = [wintypes.HICON]
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.DestroyMenu.argtypes = [wintypes.HMENU]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = ctypes.c_ssize_t


class Win32Shell:
    """托盘 + 全局热键薄壳。start() 后由独立线程维护消息循环。"""

    def __init__(
        self,
        on_open=None,
        *,
        on_workspace=None,
        on_exit=None,
        on_toggle=None,
        on_settings=None,
        on_notification=None,
        tooltip: str = "NovaMind",
        hotkey: tuple[int, int] = (MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE),
        hotkey_label: str = "Ctrl+Shift+Space",
    ) -> None:
        self._on_open = on_open
        self._on_workspace = on_workspace
        self._on_exit = on_exit
        self._on_toggle = on_toggle
        self._on_settings = on_settings
        self._on_notification = on_notification or on_workspace
        self._tooltip = tooltip
        self._hotkey = hotkey
        self._hotkey_label = hotkey_label
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd = None
        self._nid = None
        self._icon = None
        self._icon_owned = False
        self._wndproc = None
        self._wndclass = None
        self.tray_added = False
        self.hotkey_registered = False
        self.error: str | None = None

    # ── 对外 ────────────────────────────────────────────────────────

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread is not None:
            return True
        self._thread = threading.Thread(
            target=self._run, name="novamind-win32-shell", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout)
        return self.error is None

    def stop(self, timeout: float = 3.0) -> None:
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def hotkey_text(self) -> str:
        return self._hotkey_label

    # ── 线程内实现 ──────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            hinstance = _kernel32.GetModuleHandleW(None)
            from .branding import ICON_PATH
            self._icon = _user32.LoadImageW(None, str(ICON_PATH), 1, 0, 0, 0x10 | 0x40)
            self._icon_owned = bool(self._icon)
            if not self._icon:
                self._icon = _user32.LoadIconW(
                    None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR))

            self._wndproc = _WNDPROC(self._window_proc)
            self._wndclass = _WNDCLASSW()
            self._wndclass.lpfnWndProc = self._wndproc
            self._wndclass.hInstance = hinstance
            self._wndclass.hIcon = self._icon
            self._wndclass.lpszClassName = "NovaMindTrayWindow"
            if not _user32.RegisterClassW(ctypes.byref(self._wndclass)):
                raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")

            self._hwnd = _user32.CreateWindowExW(
                0, "NovaMindTrayWindow", "NovaMind", 0, 0, 0, 0, 0,
                None, None, hinstance, None,
            )
            if not self._hwnd:
                raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")

            self._add_tray()
            self._register_hotkey()
        except Exception as exc:  # pragma: no cover - 平台失败路径
            self.error = str(exc)
            self._cleanup()
            self._ready.set()
            return

        self._ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _add_tray(self) -> None:
        self._nid = _NOTIFYICONDATAW()
        self._nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self._nid.uCallbackMessage = WM_TRAY
        self._nid.hIcon = self._icon
        self._nid.szTip = self._tooltip
        self.tray_added = bool(
            _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
        )
        if self.tray_added:
            self._nid.uVersion = 4
            _shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(self._nid))

    def _register_hotkey(self) -> None:
        mods, vk = self._hotkey
        self.hotkey_registered = bool(
            _user32.RegisterHotKey(self._hwnd, 1, mods, vk)
        )
        if not self.hotkey_registered and mods & MOD_NOREPEAT:
            # 老系统不支持 MOD_NOREPEAT 时退回
            self.hotkey_registered = bool(
                _user32.RegisterHotKey(self._hwnd, 1, mods & ~MOD_NOREPEAT, vk)
            )

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_SET_HOTKEY:
            _user32.UnregisterHotKey(self._hwnd, 1)
            self._register_hotkey()
            return 0
        if msg == WM_HOTKEY:
            self._safe_call(self._on_open)
            return 0
        if msg == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event == 0x405:
                self._safe_call(self._on_notification)
            elif event == WM_LBUTTONUP:
                self._safe_call(self._on_open)
            elif event == WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._cleanup()
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd) -> None:
        _user32.SetForegroundWindow(hwnd)
        menu = _user32.CreatePopupMenu()
        _user32.AppendMenuW(menu, MF_STRING, 1, "打开完整工作台")
        _user32.AppendMenuW(menu, MF_STRING, 2, "打开快捷面板")
        _user32.AppendMenuW(menu, MF_STRING, 3, "退出")
        _user32.AppendMenuW(menu, MF_STRING, 4, "显示 / 隐藏悬浮球")
        _user32.AppendMenuW(menu, MF_STRING, 5, "个人设置")
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        command = _user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
            point.x, point.y, 0, hwnd, None,
        )
        _user32.DestroyMenu(menu)
        if command == 1:
            self._safe_call(self._on_workspace)
        elif command == 2:
            self._safe_call(self._on_open)
        elif command == 3:
            self._safe_call(self._on_exit)
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif command == 4:
            self._safe_call(self._on_toggle)
        elif command == 5:
            self._safe_call(self._on_settings)

    def set_hotkey(self, label: str) -> None:
        if label == self._hotkey_label:
            return
        parts = label.split("+")
        key = parts[-1]
        mods = MOD_NOREPEAT | (MOD_CONTROL if "Ctrl" in parts else 0) | (MOD_SHIFT if "Shift" in parts else 0) | (1 if "Alt" in parts else 0)
        vk = VK_SPACE if key == "Space" else 0x70 + int(key[1:])-1 if key.startswith("F") and len(key)>1 else ord(key)
        self._hotkey = mods, vk
        self._hotkey_label = label
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_SET_HOTKEY, 0, 0)

    def notify(self, title: str, text: str) -> bool:
        if self._nid is None:
            return False
        self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | 0x10
        self._nid.szInfoTitle = title[:63]
        self._nid.szInfo = text[:255]
        self._nid.dwInfoFlags = 1
        return bool(_shell32.Shell_NotifyIconW(1, ctypes.byref(self._nid)))

    def _cleanup(self) -> None:
        if self._nid is not None:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None
        if self._hwnd:
            _user32.UnregisterHotKey(self._hwnd, 1)
        self.tray_added = False
        self.hotkey_registered = False
        if self._icon_owned and self._icon:
            _user32.DestroyIcon(self._icon)
            self._icon = None
            self._icon_owned = False

    @staticmethod
    def _safe_call(fn) -> None:
        if fn is None:
            return
        try:
            fn()
        except Exception:  # pragma: no cover - 回调异常不拖垮消息循环
            pass
