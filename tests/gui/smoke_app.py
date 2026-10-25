"""Opt-in GUI fixture: isolated workspace, offline model, loopback-only network.

Browser: python tests/gui/smoke_app.py --port 18765
Native lifecycle: python tests/gui/smoke_app.py --native --port 18766
Never reads or modifies the user's workspace or single-instance lock.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def only_loopback() -> None:
    original = socket.socket.connect

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else ""
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise AssertionError("GUI smoke blocks all non-loopback networking")
        return original(sock, address)

    socket.socket.connect = connect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--hidden", action="store_true", help="native startup-hidden regression")
    args = parser.parse_args()
    workspace = ROOT / "build" / "ui-refinement" / ("native-" if args.native else "browser-")
    workspace = workspace.with_name(workspace.name + uuid.uuid4().hex[:8])
    workspace.mkdir(parents=True)
    os.environ["NOVAMIND_WORKSPACE"] = str(workspace)
    only_loopback()

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from novamind.webui import server
    from novamind.webui.runtime import WebRuntime

    class OfflineChat(BaseChatModel):
        @property
        def _llm_type(self):
            return "offline-gui-smoke"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            # Slow enough to exercise stop/disabled controls without model costs.
            time.sleep(.4)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="离线界面测试回答：已收到你的消息，历史会保存在独立测试工作区。"))])

    server._runtime_factory = lambda: WebRuntime(llm=OfflineChat())
    from novamind.core import provider

    def forbidden_provider(*args, **kwargs):
        raise AssertionError("A real provider must never be built by the GUI smoke")

    provider.get_provider = forbidden_provider
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    async def quick_layout(request):
        return HTMLResponse((Path(__file__).with_name("quick_layout.html")).read_text(encoding="utf-8"))

    server.app.routes.insert(0, Route("/__smoke__/quick", quick_layout))
    print("OFFLINE_WORKSPACE=" + str(workspace), flush=True)
    if not args.native:
        import uvicorn
        from novamind.webui.local_guard import set_local_guard_enabled
        set_local_guard_enabled(True)
        uvicorn.run(server.app, host="127.0.0.1", port=args.port, log_level="warning")
        return 0

    import webview
    from novamind.core.personal import settings, update_settings
    from novamind.desktop.shell import DesktopShell
    from novamind.webui.app import _find_available_url
    shell = DesktopShell(port=args.port)
    shell._server, shell._url = _find_available_url("127.0.0.1", args.port, local_guard=True)
    update_settings("desktop", {"open_workspace": False, "motion": False, "bubble_visible": not args.hidden})
    shell._create_windows()
    results = []

    def check(name, condition):
        assert condition, name
        results.append(name)
        print("PASS " + name, flush=True)

    def native_checks():
        try:
            # Exercise the real early-start ordering, not only already-loaded forms.
            shell._after_start()
            for window in (shell._bubble, shell._panel, shell._workspace, shell._menu):
                assert window.events.loaded.wait(30), "page did not load: " + window.title
            from novamind.desktop.branding import ICON_PATH, colorref
            from System.Drawing import Icon
            with Icon(str(ICON_PATH)) as expected_icon:
                with expected_icon.ToBitmap() as expected:
                    expected_color = expected.GetPixel(expected.Width//2, expected.Height//2).ToArgb()
                for window in (shell._bubble, shell._panel, shell._workspace, shell._menu):
                    with window.native.Icon.ToBitmap() as actual:
                        check('native icon is NovaMind: ' + window.title,
                              actual.GetPixel(actual.Width//2, actual.Height//2).ToArgb() == expected_color)
            for theme, background in (('light', '#f8faff'), ('dark', '#111827')):
                shell._workspace.evaluate_js("document.documentElement.dataset.theme=" + json.dumps(theme))
                deadline = time.monotonic() + 5
                while getattr(shell._workspace, '_novamind_chrome', (None, None))[1] != theme and time.monotonic() < deadline:
                    time.sleep(.05)
                signature = getattr(shell._workspace, '_novamind_chrome', None)
                applied = getattr(shell._workspace, '_novamind_chrome_results', {})
                print('NATIVE_CHROME', theme, applied, signature, flush=True)
                check('native titlebar accepts ' + theme + ' workspace colors',
                      signature[2] == colorref(background) and applied.get(35) == 0 and applied.get(36) == 0)
            alpha_deadline = time.monotonic() + 15
            while not getattr(shell._bubble, '_novamind_alpha_surface', False) and time.monotonic() < alpha_deadline:
                time.sleep(.1)
            check('bubble uses per-pixel alpha', getattr(shell._bubble, '_novamind_alpha_surface', False))
            frame = shell._bubble._novamind_surface._frame
            alpha = frame.getchannel('A')
            check('bubble frame has transparent corners and smooth edge pixels',
                  alpha.getpixel((0, 0)) == 0 and any(0 < value < 255 for value in alpha.tobytes()))
            before_theme = frame.tobytes()
            current_theme = shell._bubble.evaluate_js('document.documentElement.dataset.theme')
            update_settings('desktop', {'theme': 'dark' if current_theme == 'light' else 'light'})
            theme_deadline = time.monotonic() + 10
            while shell._bubble._novamind_surface._frame.tobytes() == before_theme and time.monotonic() < theme_deadline:
                time.sleep(.1)
            check('native bubble refreshes after theme changes',
                  shell._bubble._novamind_surface._frame.tobytes() != before_theme)
            check("startup visibility matches preference", shell._bubble.native.Visible == (not args.hidden))
            if args.hidden:
                shell.toggle_bubble()
                check("hidden startup can be shown", shell._bubble.native.Visible)
            shell.toggle_bubble()
            check("hide bubble", not shell._bubble.native.Visible)
            update_settings("desktop", {"size": 56})
            shell._apply_desktop_settings(settings()["desktop"])
            check("hidden bubble survives resize", not shell._bubble.native.Visible)
            shell._on_bubble_loaded()
            check("hidden bubble survives page-ready", not shell._bubble.native.Visible)
            shell.reset_bubble()
            check("recover bubble", shell._bubble.native.Visible)
            area = shell._work_area(shell._bubble.x, shell._bubble.y)
            print("GEOMETRY", shell._bubble.x, shell._bubble.y, shell._bubble.width,
                  shell._bubble.height, area, "native", shell._bubble.native.Bounds, flush=True)
            check("bubble on-screen", area[0] <= shell._bubble.x <= area[2]-shell._bubble.width and
                  area[1] <= shell._bubble.y <= area[3]-shell._bubble.height)
            for iteration in range(3):
                shell._bridge.show_panel("native-smoke-thread")
                check(f"panel open {iteration+1}", shell._panel.native.Visible and shell._panel_open)
                shell.hide_panel()
                check(f"panel hidden {iteration+1}", not shell._panel.native.Visible)
            shell.show_menu()
            check("menu visible and tracked", shell._menu.native.Visible and shell._menu_visible)
            shell.show_menu()
            check("repeated menu open stays visible", shell._menu.native.Visible and shell._menu_visible)
            shell.hide_menu()
            check("menu explicitly closes", not shell._menu.native.Visible and not shell._menu_visible)
            shell.open_workspace()
            check("workspace opens", shell._workspace.native.Visible)
            shell._on_workspace_closing()
            check("workspace close keeps bubble alive", not shell._workspace.native.Visible and shell._bubble.native.Visible)
            shell._bridge.show_panel()
            check("panel opens after workspace hidden", shell._panel.native.Visible)
            import urllib.request
            with urllib.request.urlopen(shell._url + "/health", timeout=3) as response:
                check("backend responds after native lifecycle", response.status == 200)
            result = {"ok": True, "checks": results}
        except Exception as error:
            import traceback
            traceback.print_exc()
            result = {"ok": False, "checks": results, "error": str(error)}
        finally:
            (workspace / "native-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            shell.quit()

    # A bounded watchdog closes only this fixture's windows if initialization stalls.
    timer = threading.Timer(100, shell.quit)
    timer.daemon = True
    timer.start()
    try:
        from novamind.desktop.branding import ICON_PATH
        webview.start(native_checks, debug=False, icon=str(ICON_PATH))
    finally:
        timer.cancel()
        shell._server.should_exit = True
        shell._server.novamind_thread.join(timeout=10)
    result_file = workspace / "native-result.json"
    return 0 if result_file.exists() and json.loads(result_file.read_text(encoding="utf-8"))["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
