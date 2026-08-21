"""NovaMind GUI 打包入口（pyinstaller 用）。

运行：nova-mind-gui.exe
"""

from novamind.webui.app import run_gui


def main() -> None:
    run_gui()


if __name__ == "__main__":
    main()
