"""Native app identity and theme-aligned Windows chrome; no business state."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import re

from .ui_thread import post_to_ui

logger = logging.getLogger(__name__)
ICON_PATH = Path(__file__).with_name('assets') / 'novamind.ico'
APP_ID = 'NovaMind.PersonalAgent'
PALETTES = {'light': ('#f8faff', '#14213a'), 'dark': ('#111827', '#e5edf9')}


def set_app_identity() -> None:
    api = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    api.argtypes = [wintypes.LPCWSTR]
    api.restype = ctypes.c_long
    if api(APP_ID) < 0:
        logger.warning('could not set NovaMind taskbar identity')


def resolve_theme(theme: str) -> str:
    if theme in PALETTES:
        return theme
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            return 'light' if winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] else 'dark'
    except OSError:
        return 'light'


def colorref(color: str) -> int:
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('invalid window color')
    red, green, blue = (int(color[index:index+2], 16) for index in (1, 3, 5))
    return red | green << 8 | blue << 16


def apply_window_theme(window, theme: str, background: str | None = None,
                       foreground: str | None = None) -> bool:
    if window is None or theme not in PALETTES:
        return False
    default_bg, default_fg = PALETTES[theme]
    background, foreground = background or default_bg, foreground or default_fg
    bg, fg = colorref(background), colorref(foreground)

    def apply():
        native = window.native
        handle = native.Handle.ToInt64()
        signature = handle, theme, bg, fg
        if getattr(window, '_novamind_chrome', None) == signature:
            return
        api = ctypes.windll.dwmapi.DwmSetWindowAttribute
        api.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        api.restype = ctypes.c_long
        # Caption and client header use the same tokens. NONE removes the
        # contrasting DWM outline while preserving native resize/snap behavior.
        results = {}
        for attribute, value in ((20, int(theme == 'dark')), (34, 0xFFFFFFFE), (35, bg), (36, fg)):
            payload = wintypes.DWORD(value)
            result = api(handle, attribute, ctypes.byref(payload), ctypes.sizeof(payload))
            results[attribute] = result
            if result < 0:
                logger.debug('window theme attribute %s unavailable: %s', attribute, result)
        from System.Drawing import ColorTranslator
        native.BackColor = ColorTranslator.FromHtml(background)
        window._novamind_chrome = signature
        window._novamind_chrome_results = results

    return post_to_ui(window, apply)
