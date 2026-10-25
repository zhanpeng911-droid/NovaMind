"""Native work areas converted into pywebview's per-window logical coordinates."""
from __future__ import annotations

import ctypes
from ctypes import wintypes


def window_scale(window) -> float:
    try:
        handle = window.native.Handle.ToInt64()
        api = ctypes.windll.user32.GetDpiForWindow
        api.argtypes = [wintypes.HWND]
        api.restype = wintypes.UINT
        return max(96, api(handle)) / 96
    except (AttributeError, OSError, TypeError):
        return 1.0


def is_visible(window) -> bool | None:
    """Native visibility, not the creation-time pywebview `hidden` option."""
    try:
        handle = window.native.Handle.ToInt64()
        api = ctypes.windll.user32.IsWindowVisible
        api.argtypes = [wintypes.HWND]
        api.restype = wintypes.BOOL
        return bool(api(handle))
    except (AttributeError, OSError, TypeError):
        return None


def clip_window(window, *, circle: bool = False, inset: int = 8) -> None:
    """Clip the native host too: WebView transparency alone leaves a Form box."""
    from .ui_thread import post_to_ui

    post_to_ui(window, lambda: _apply_clip(window, circle=circle, inset=inset))


def _apply_clip(window, *, circle: bool, inset: int) -> None:
    """Only called on the owning UI thread; region changes can raise Resize."""
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    handle = window.native.Handle.ToInt64()
    scale = window_scale(window)
    rect = wintypes.RECT()
    user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    if not user.GetClientRect(handle, ctypes.byref(rect)):
        raise OSError("could not obtain desktop window bounds")
    padding = round(inset * scale)
    bounds = (padding, padding, rect.right-padding, rect.bottom-padding)
    if bounds[2] <= padding or bounds[3] <= padding:
        return
    signature = (handle, bounds, circle, scale)
    if getattr(window, "_novamind_clip_bounds", None) == signature:
        return
    create = gdi.CreateEllipticRgn if circle else gdi.CreateRoundRectRgn
    create.argtypes = [ctypes.c_int] * (4 if circle else 6)
    create.restype = wintypes.HANDLE
    region = create(*bounds) if circle else create(*bounds, round(34*scale), round(34*scale))
    if not region:
        raise OSError("could not create desktop window region")
    user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
    user.SetWindowRgn.restype = wintypes.BOOL
    if not user.SetWindowRgn(handle, region, True):
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi.DeleteObject(region)
        raise OSError("could not clip desktop window region")
    # A successful SetWindowRgn transfers region ownership to Windows.
    window._novamind_clip_bounds = signature


def work_area(window, x: int, y: int) -> tuple[int, int, int, int]:
    class Info(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    user = ctypes.windll.user32
    scale = window_scale(window)
    user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user.MonitorFromPoint.restype = wintypes.HANDLE
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    handle = user.MonitorFromPoint(wintypes.POINT(round(x * scale), round(y * scale)), 2)
    info = Info()
    info.size = ctypes.sizeof(info)
    if user.GetMonitorInfoW(handle, ctypes.byref(info)):
        return tuple(round(value / scale) for value in
                     (info.work.left, info.work.top, info.work.right, info.work.bottom))
    return 0, 0, 1920, 1080


def clamp(x: int, y: int, width: int, height: int, area: tuple[int, int, int, int]):
    left, top, right, bottom = area
    return max(left, min(int(x), right-width)), max(top, min(int(y), bottom-height))
