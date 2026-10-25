"""Native branding contracts without opening a window or calling a model."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from novamind.desktop import branding


def test_native_icon_contains_small_and_high_dpi_sizes():
    with Image.open(branding.ICON_PATH) as image:
        assert {(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (256, 256)} <= image.ico.sizes()
        assert image.convert('RGBA').getpixel((0, 0))[3] == 0


@pytest.mark.parametrize('color', ['', 'red', '#abcd', '#00000000', 'rgb(0,0,0)', None])
def test_invalid_native_colors_are_rejected(color):
    with pytest.raises(ValueError):
        branding.colorref(color)


def test_colorref_uses_windows_bgr_order():
    assert branding.colorref('#14213a') == 0x3A2114
    assert branding.colorref('#f8faff') == 0xFFFAF8


def test_titlebar_mutations_are_queued_and_cached(monkeypatch):
    import ctypes
    pending, values = [], []
    window = SimpleNamespace(native=SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 123)))
    monkeypatch.setattr(branding, 'post_to_ui', lambda target, callback: pending.append(callback) or True)
    api = Mock(side_effect=lambda handle, attr, pointer, size: values.append((attr, pointer._obj.value)) or 0)
    monkeypatch.setattr(ctypes.windll, 'dwmapi', SimpleNamespace(DwmSetWindowAttribute=api))
    monkeypatch.setitem(sys.modules, 'System.Drawing', SimpleNamespace(
        ColorTranslator=SimpleNamespace(FromHtml=lambda value: value)))
    assert branding.apply_window_theme(window, 'dark')
    assert not values
    pending.pop()()
    assert values == [(20, 1), (34, 0xFFFFFFFE), (35, 0x271811), (36, 0xF9EDE5)]
    assert window.native.BackColor == '#111827'
    branding.apply_window_theme(window, 'dark')
    pending.pop()()
    assert len(values) == 4


def test_unknown_theme_does_not_touch_a_window():
    assert not branding.apply_window_theme(SimpleNamespace(), 'unknown')
