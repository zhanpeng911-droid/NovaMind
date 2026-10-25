"""Alpha compositing and native bubble action contracts; no real model calls."""
import base64
import io
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image, ImageDraw
import pytest

from novamind.desktop.bubble_surface import BubbleSurface, decode_frame, premultiplied_pixels


def test_antialias_alpha_is_preserved_and_colors_are_premultiplied():
    image = Image.new('RGBA', (128, 128))
    ImageDraw.Draw(image).ellipse((5, 5, 122, 122), fill=(200, 100, 50, 255))
    pixels = premultiplied_pixels(image, (32, 32))
    alpha = pixels[3::4]
    assert alpha[0] == 0
    assert 255 in alpha
    assert any(0 < value < 255 for value in alpha)
    for start in range(0, len(pixels), 4):
        blue, green, red, opacity = pixels[start:start+4]
        assert max(blue, green, red) <= opacity


def test_transparent_png_decoding_does_not_flatten_the_frame():
    image = Image.new('RGBA', (32, 32), (10, 20, 30, 0))
    image.putpixel((16, 16), (100, 80, 40, 128))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    decoded = decode_frame('data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode())
    assert decoded.getpixel((0, 0))[3] == 0
    assert premultiplied_pixels(decoded, (32, 32))[(16*32+16)*4:][:4] == bytes((20, 40, 50, 128))


@pytest.mark.parametrize('frame', ['', 'data:image/svg+xml;base64,PHN2Zz4=',
                                  'data:image/png;base64,not-base64', 'x' * 1_400_001],
                         ids=['empty', 'wrong-format', 'invalid-base64', 'oversized'])
def test_invalid_frames_are_rejected(frame):
    with pytest.raises(ValueError):
        decode_frame(frame)


def input_surface():
    surface = object.__new__(BubbleSurface)
    surface.shell = SimpleNamespace(show_menu=Mock(), show_panel=Mock(), drag_ball=Mock())
    surface._worker = lambda action: action()
    surface._origin = (10, 10)
    surface._dragged = False
    surface._last_delta = (0, 0)
    surface._queue_move = Mock()
    return surface


def test_native_left_click_and_right_click_keep_their_actions():
    surface = input_surface()
    native = SimpleNamespace(Capture=True)
    surface._up(native, SimpleNamespace(Button='Left'))
    surface.shell.show_panel.assert_called_once_with()
    assert not native.Capture
    surface._up(native, SimpleNamespace(Button='Right'))
    surface.shell.show_menu.assert_called_once_with()


def test_drag_finishes_without_opening_the_panel():
    surface = input_surface()
    surface._dragged, surface._last_delta = True, (20, 35)
    surface._up(SimpleNamespace(Capture=True), SimpleNamespace(Button='Left'))
    surface._queue_move.assert_called_once_with(20, 35, True)
    surface.shell.show_panel.assert_not_called()


def test_keyboard_panel_and_menu_actions_are_preserved():
    surface = input_surface()
    for name in ('Enter', 'Space'):
        event = SimpleNamespace(KeyCode=name, Shift=False, Handled=False)
        surface._key(None, event)
        assert event.Handled
    surface._key(None, SimpleNamespace(KeyCode='F10', Shift=True, Handled=False))
    assert surface.shell.show_panel.call_count == 2
    surface.shell.show_menu.assert_called_once_with()
