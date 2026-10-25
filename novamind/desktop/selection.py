"""Windows 划词捕获（A2，best-effort）。

策略：保存当前剪贴板文本快照 → 模拟 Ctrl+C → 等待剪贴板变化 → 读取；
若剪贴板仍等于捕获值则恢复旧内容，避免覆盖用户并发修改。
限制：只在前台应用支持 Ctrl+C 复制时有效；失败返回空串，UI 回退到手动粘贴。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - 平台分支
    raise ImportError("selection capture is Windows-only")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_C = 0x43
KEYEVENTF_KEYUP = 0x0002

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.EmptyClipboard.restype = wintypes.BOOL
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL


def read_clipboard_text() -> str:
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    if not _user32.OpenClipboard(None):
        return ""
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def write_clipboard_text(text: str) -> bool:
    encoded = (text + "\0").encode("utf-16-le")
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not handle:
        return False
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(pointer, encoded, len(encoded))
    _kernel32.GlobalUnlock(handle)
    if not _user32.OpenClipboard(None):
        _kernel32.GlobalFree(handle)
        return False
    transferred = False
    try:
        if not _user32.EmptyClipboard():
            return False
        # SetClipboardData 接管 handle 所有权，不能再次 GlobalFree
        transferred = bool(_user32.SetClipboardData(CF_UNICODETEXT, handle))
        return transferred
    finally:
        _user32.CloseClipboard()
        if not transferred:
            _kernel32.GlobalFree(handle)


def _send_copy() -> None:
    _user32.keybd_event(VK_CONTROL, 0, 0, 0)
    _user32.keybd_event(VK_C, 0, 0, 0)
    _user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    _user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def capture_selection(timeout: float = 0.4) -> str:
    """best-effort 捕获前台选中文本；失败返回空串。"""
    # Rich clipboard formats cannot be faithfully restored by a text-only
    # copier. Keep them intact and let the user explicitly paste instead.
    allowed = {1, 13, 16}
    if not _user32.OpenClipboard(None):
        return ""
    try:
        current = 0
        while True:
            current = _user32.EnumClipboardFormats(current)
            if not current:
                break
            if current not in allowed:
                return ""
    finally:
        _user32.CloseClipboard()
    previous = read_clipboard_text()
    if not previous:
        return ""
    _send_copy()
    deadline = time.monotonic() + timeout
    captured = ""
    while time.monotonic() < deadline:
        time.sleep(0.02)
        current = read_clipboard_text()
        if current and current != previous:
            captured = current
            break
    if not captured:
        return ""
    # 仅当剪贴板仍是本次捕获内容时才恢复，避免覆盖用户并发修改
    if previous and read_clipboard_text() == captured:
        write_clipboard_text(previous)
    return captured


def foreground_window():
    _user32.GetForegroundWindow.restype = wintypes.HWND
    return _user32.GetForegroundWindow()


def activate_window(hwnd) -> bool:
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    result = bool(_user32.SetForegroundWindow(hwnd))
    if result:
        time.sleep(.05)
    return result
