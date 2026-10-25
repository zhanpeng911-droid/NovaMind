"""NovaMind 打包入口（pyinstaller 用）。

- Windows 默认：同一程序启动工作台、托盘、悬浮球、快捷窗口和日程提醒。
- 其他系统：完整工作台 GUI（pywebview）。
"""

from __future__ import annotations

import sys


def main() -> None:
    desktop_mode = sys.platform == "win32"
    if desktop_mode:
        from novamind.desktop.shell import DesktopShell

        raise SystemExit(DesktopShell().run())

    from novamind.webui.app import run_gui

    run_gui()


if __name__ == "__main__":
    main()
