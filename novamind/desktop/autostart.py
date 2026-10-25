"""Opt-in Windows startup for the packaged app, never a second background service."""
from __future__ import annotations

import sys


def configure(enabled: bool) -> None:
    if sys.platform != "win32":
        raise ValueError("开机自启仅支持 Windows")
    if enabled and not getattr(sys, "frozen", False):
        raise ValueError("请在打包程序内开启自启，源码运行不写入不稳定的启动路径")
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "NovaMind", 0, winreg.REG_SZ, '"' + sys.executable + '"')
        else:
            try:
                winreg.DeleteValue(key, "NovaMind")
            except FileNotFoundError:
                pass
