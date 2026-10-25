"""Native mutations are queued to the UI, never synchronous worker setters."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from novamind.desktop.ui_thread import post_to_ui


def native_window(**changes):
    native = SimpleNamespace(IsDisposed=False, IsHandleCreated=True,
                             InvokeRequired=False, BeginInvoke=MagicMock(), **changes)
    return SimpleNamespace(native=native)


def test_worker_queues_without_waiting_for_the_ui(monkeypatch):
    window = native_window()
    window.native.InvokeRequired = True
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda fn: fn))
    mutation = MagicMock()
    assert post_to_ui(window, mutation)
    mutation.assert_not_called()
    window.native.BeginInvoke.call_args.args[0]()
    mutation.assert_called_once_with()


def test_ui_thread_applies_directly():
    window = native_window()
    mutation = MagicMock()
    assert post_to_ui(window, mutation)
    mutation.assert_called_once_with()
    window.native.BeginInvoke.assert_not_called()


@pytest.mark.parametrize("field", ["IsDisposed", "IsHandleCreated"])
def test_unready_or_closed_windows_are_not_mutated(field):
    window = native_window()
    setattr(window.native, field, field == "IsDisposed")
    mutation = MagicMock()
    assert not post_to_ui(window, mutation)
    mutation.assert_not_called()


def test_queued_operation_does_not_touch_a_disposed_window(monkeypatch):
    window = native_window()
    window.native.InvokeRequired = True
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda fn: fn))
    mutation = MagicMock()
    post_to_ui(window, mutation)
    window.native.IsDisposed = True
    window.native.BeginInvoke.call_args.args[0]()
    mutation.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="native Win32 region API")
def test_region_resize_event_does_not_reapply_identical_region(monkeypatch):
    import ctypes

    from novamind.desktop.geometry import clip_window

    window = native_window(Handle=SimpleNamespace(ToInt64=lambda: 123))
    size = [64, 64]

    def bounds(handle, pointer):
        pointer._obj.right, pointer._obj.bottom = size
        return 1

    user = SimpleNamespace(GetClientRect=MagicMock(side_effect=bounds),
                           GetDpiForWindow=MagicMock(return_value=96),
                           SetWindowRgn=MagicMock(return_value=1))
    gdi = SimpleNamespace(CreateEllipticRgn=MagicMock(return_value=456))
    monkeypatch.setattr(ctypes.windll, "user32", user)
    monkeypatch.setattr(ctypes.windll, "gdi32", gdi)
    clip_window(window, circle=True)
    clip_window(window, circle=True)
    assert user.SetWindowRgn.call_count == 1
    size[:] = [72, 72]
    clip_window(window, circle=True)
    assert user.SetWindowRgn.call_count == 2
