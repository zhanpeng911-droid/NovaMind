"""Per-pixel-alpha bubble, retaining the existing WebView and shell lifecycle.

The hidden WebView supplies a supersampled PNG of the same SVG/custom icon.
Win32 composites its alpha instead of cutting a binary, jagged window region.
All native mutations run on the owning UI thread; actions run on workers.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import io
import logging
import threading

from PIL import Image

from .geometry import window_scale
from .ui_thread import post_to_ui

logger = logging.getLogger(__name__)
_LAYERED = 0x00080000


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                ("height", wintypes.LONG), ("planes", wintypes.WORD),
                ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                ("image_size", wintypes.DWORD), ("x_resolution", wintypes.LONG),
                ("y_resolution", wintypes.LONG), ("colors", wintypes.DWORD),
                ("important", wintypes.DWORD), ("palette", wintypes.DWORD)]


class _Blend(ctypes.Structure):
    _fields_ = [("operation", ctypes.c_ubyte), ("flags", ctypes.c_ubyte),
                ("opacity", ctypes.c_ubyte), ("alpha_format", ctypes.c_ubyte)]


def decode_frame(data: str) -> Image.Image:
    prefix = "data:image/png;base64,"
    if not isinstance(data, str) or not data.startswith(prefix) or len(data) > 1_400_000:
        raise ValueError("invalid bubble frame")
    raw = base64.b64decode(data[len(prefix):], validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "PNG" or max(image.size) > 1024 or min(image.size) < 16:
            raise ValueError("invalid bubble dimensions")
        return image.convert("RGBA")


def premultiplied_pixels(image: Image.Image, size: tuple[int, int]) -> bytes:
    """Win32 requires top-down BGRA with RGB already multiplied by alpha."""
    return image.resize(size, Image.Resampling.LANCZOS).convert("RGBa").tobytes("raw", "BGRa")


class BubbleSurface:
    def __init__(self, window, shell):
        self.window, self.shell = window, shell
        self._ready = False
        self._frame = None
        self._painting = False
        self._origin = None
        self._dragged = False
        self._last_delta = (0, 0)
        self._move_lock = threading.Lock()
        self._pending_move = None
        self._moving = False
        self._refreshing = threading.Event()
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self._user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        self._user.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.POINTER(wintypes.SIZE),
            wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
            ctypes.POINTER(_Blend), wintypes.DWORD,
        ]
        self._gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self._gdi.CreateCompatibleDC.restype = wintypes.HDC
        self._gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfo),
                                             wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                             wintypes.HANDLE, wintypes.DWORD]
        self._gdi.CreateDIBSection.restype = wintypes.HANDLE
        self._gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self._gdi.SelectObject.restype = wintypes.HANDLE
        self._gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        self._gdi.DeleteDC.argtypes = [wintypes.HDC]

    def present(self, data: str) -> bool:
        frame = decode_frame(data)
        return post_to_ui(self.window, lambda: self._apply(frame))

    def _apply(self, frame) -> None:
        if self._painting:
            return
        native = self.window.native
        if native.IsDisposed or not native.IsHandleCreated:
            return
        self._painting = True
        handle = native.Handle.ToInt64()
        previous_style = self._user.GetWindowLongPtrW(handle, -20)
        try:
            if not self._ready:
                # WinForms used whole-window opacity during hidden creation.
                # Clear/reapply WS_EX_LAYERED before UpdateLayeredWindow.
                self._user.SetWindowLongPtrW(handle, -20, previous_style & ~_LAYERED)
                self._user.SetWindowLongPtrW(handle, -20, previous_style | _LAYERED)
                native.browser.webview.Visible = False
            rect = wintypes.RECT()
            if not self._user.GetClientRect(handle, ctypes.byref(rect)):
                raise ctypes.WinError(ctypes.get_last_error())
            size = (rect.right, rect.bottom)
            if min(size) <= 0 or max(size) > 1024:
                return
            pixels = premultiplied_pixels(frame, size)
            self._blit(handle, size, pixels)
            if not self._ready:
                self._user.SetWindowRgn(handle, None, True)
                self.window._novamind_alpha_surface = True
                self._attach_input(native)
                self._ready = True
            self._frame = frame
        except Exception:
            if not self._ready:
                self._user.SetWindowLongPtrW(handle, -20, previous_style & ~_LAYERED)
                native.browser.webview.Visible = True
            raise
        finally:
            self._painting = False

    def _blit(self, handle, size, pixels):
        dc = self._gdi.CreateCompatibleDC(None)
        if not dc:
            raise ctypes.WinError(ctypes.get_last_error())
        bitmap = previous = None
        try:
            info = _BitmapInfo()
            info.size = 40
            info.width, info.height = size[0], -size[1]
            info.planes, info.bits = 1, 32
            address = ctypes.c_void_p()
            bitmap = self._gdi.CreateDIBSection(dc, ctypes.byref(info), 0,
                                               ctypes.byref(address), None, 0)
            if not bitmap or not address.value:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.memmove(address, pixels, len(pixels))
            previous = self._gdi.SelectObject(dc, bitmap)
            dimensions, origin, blend = wintypes.SIZE(*size), wintypes.POINT(0, 0), _Blend(0, 0, 255, 1)
            if not self._user.UpdateLayeredWindow(handle, None, None, ctypes.byref(dimensions),
                                                  dc, ctypes.byref(origin), 0, ctypes.byref(blend), 2):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if previous:
                self._gdi.SelectObject(dc, previous)
            if bitmap:
                self._gdi.DeleteObject(bitmap)
            self._gdi.DeleteDC(dc)

    @staticmethod
    def _worker(action):
        def run():
            try:
                action()
            except Exception:
                logger.exception("bubble input action failed")
        threading.Thread(target=run, name="bubble-input", daemon=True).start()

    def _attach_input(self, native):
        from System.Windows.Forms import AccessibleRole, Cursors
        native.Cursor = Cursors.Hand
        native.AccessibleName = "打开 NovaMind；右键显示快捷菜单"
        native.AccessibleRole = AccessibleRole.PushButton
        native.MouseDown += self._down
        native.MouseMove += self._move
        native.MouseUp += self._up
        native.MouseCaptureChanged += self._capture_changed
        native.KeyPreview = True
        native.KeyDown += self._key
        native.MouseEnter += lambda *_: self._hover(True)
        native.MouseLeave += lambda *_: self._hover(False)
        native.Resize += self._resize
        # A hidden WebView may throttle timers/animation frames. Drive refresh
        # from the native UI timer so saved appearance/status stays current.
        from System.Windows.Forms import Timer
        self._timer = Timer()
        self._timer.Interval = 1500
        self._timer.Tick += self._refresh
        native.FormClosed += lambda *_: self._timer.Dispose()
        self._timer.Start()

    def _refresh(self, *_):
        if self._refreshing.is_set():
            return
        self._refreshing.set()

        def refresh():
            try:
                self.window.evaluate_js('window.novaMindNativeBubble=true; window.novaMindRefreshBubble?.()')
            finally:
                self._refreshing.clear()
        self._worker(refresh)

    def _resize(self, *_):
        if self._frame is not None:
            try:
                self._apply(self._frame)
            except Exception:
                logger.exception('could not resize bubble surface')

    def _key(self, sender, event):
        name = str(event.KeyCode)
        if name in ('Return', 'Enter', 'Space'):
            event.Handled = True
            self._worker(self.shell.show_panel)
        elif name == 'Apps' or (name == 'F10' and event.Shift):
            event.Handled = True
            self._worker(self.shell.show_menu)

    def _hover(self, active):
        script = "window.novaMindBubbleHover?.(" + ("true" if active else "false") + ")"
        self._worker(lambda: self.window.evaluate_js(script))

    @staticmethod
    def _pointer():
        from System.Windows.Forms import Cursor
        point = Cursor.Position
        return point.X, point.Y

    def _down(self, sender, event):
        if str(event.Button) == "Left":
            self._origin, self._dragged = self._pointer(), False
            self._last_delta = (0, 0)
            sender.Capture = True

    def _move(self, sender, event):
        if self._origin is None:
            return
        x, y = self._pointer()
        scale = window_scale(self.window)
        dx, dy = round((x-self._origin[0])/scale), round((y-self._origin[1])/scale)
        self._last_delta = dx, dy
        self._dragged = self._dragged or dx*dx + dy*dy > 25
        if self._dragged:
            self._queue_move(dx, dy, False)

    def _up(self, sender, event):
        if str(event.Button) == "Right":
            self._worker(self.shell.show_menu)
        elif str(event.Button) == "Left" and self._origin is not None:
            dragged, delta = self._dragged, self._last_delta
            self._origin = None
            sender.Capture = False
            if dragged:
                self._queue_move(*delta, True)
            else:
                self._worker(self.shell.show_panel)

    def _capture_changed(self, sender, event):
        if self._origin is not None and not sender.Capture:
            self._origin = None
            if self._dragged:
                self._queue_move(*self._last_delta, True)

    def _queue_move(self, dx, dy, finished):
        with self._move_lock:
            self._pending_move = dx, dy, finished
            if self._moving:
                return
            self._moving = True
        self._worker(self._drain_moves)

    def _drain_moves(self):
        try:
            while True:
                with self._move_lock:
                    move, self._pending_move = self._pending_move, None
                    if move is None:
                        self._moving = False
                        return
                self.shell.drag_ball(*move)
        except Exception:
            with self._move_lock:
                self._moving = False
            raise
