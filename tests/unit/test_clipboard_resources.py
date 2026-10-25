"""Clipboard ownership on failure; mocks never touch the real clipboard."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

selection = pytest.importorskip('novamind.desktop.selection')


@pytest.mark.parametrize('opened,emptied,accepted', [(False, True, True),
                                                   (True, False, True),
                                                   (True, True, False),
                                                   (True, True, True)])
def test_clipboard_memory_is_freed_unless_transferred(monkeypatch, opened, emptied, accepted):
    memory = ctypes.create_string_buffer(128)
    user = SimpleNamespace(OpenClipboard=Mock(return_value=opened), EmptyClipboard=Mock(return_value=emptied),
                           SetClipboardData=Mock(return_value=456 if accepted else 0), CloseClipboard=Mock())
    kernel = SimpleNamespace(GlobalAlloc=Mock(return_value=456),
                             GlobalLock=Mock(return_value=ctypes.addressof(memory)),
                             GlobalUnlock=Mock(), GlobalFree=Mock())
    monkeypatch.setattr(selection, '_user32', user)
    monkeypatch.setattr(selection, '_kernel32', kernel)
    transferred = opened and emptied and accepted
    assert selection.write_clipboard_text('你好') == transferred
    assert kernel.GlobalFree.call_count == (0 if transferred else 1)
    assert user.CloseClipboard.call_count == int(opened)
    assert user.SetClipboardData.call_count == int(opened and emptied)
