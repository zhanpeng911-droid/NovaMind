"""A0 桌面可行性 Spike / 冒烟自测。

用法：
    python -m novamind.desktop.spike --mode pywebview --self-test
    python -m novamind.desktop.spike --mode pyside --self-test
    python -m novamind.desktop.spike --smoke        # A0 门禁：单实例 + 守卫 + 托盘/热键 + 后端生命周期

输出 JSON 实测数据（导入耗时、窗口创建耗时、RSS、托盘/热键是否成功），
供 ADR 决策；不修改现有主 GUI。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import socket
import sys
import threading
import time

_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>body{font-family:Segoe UI;margin:0;background:#12141a;color:#e6e8ee;
display:flex;align-items:center;justify-content:center;height:100vh}
.card{padding:18px 22px;border:1px solid #2a2f3a;border-radius:14px;background:#171a22}
small{color:#8b93a7}</style></head><body>
<div class="card"><b>NovaMind Quick Panel</b><br><small>A0 spike · 无模型调用</small></div>
</body></html>"""


def _rss_mb() -> float:
    if sys.platform != "win32":
        return 0.0

    class _PMC(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    counters = _PMC()
    counters.cb = ctypes.sizeof(counters)
    psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    return round(counters.WorkingSetSize / 1024 / 1024, 1)


def _import_seconds(module_name: str) -> tuple[float, str | None]:
    start = time.perf_counter()
    try:
        __import__(module_name)
    except Exception as exc:  # pragma: no cover - 环境相关
        return round(time.perf_counter() - start, 3), str(exc)
    return round(time.perf_counter() - start, 3), None


def run_pywebview_selftest(hold_seconds: float = 1.2) -> dict:
    import_seconds, error = _import_seconds("webview")
    if error:
        return {"mode": "pywebview", "available": False, "error": error}

    import webview

    from .win32_shell import Win32Shell

    result: dict = {"mode": "pywebview", "available": True, "import_seconds": import_seconds}
    shell = Win32Shell(on_open=lambda: None, tooltip="NovaMind Spike")
    start = time.perf_counter()
    result["shell_started"] = shell.start()
    result["shell_start_seconds"] = round(time.perf_counter() - start, 3)
    result["tray_added"] = shell.tray_added
    result["hotkey_registered"] = shell.hotkey_registered
    result["hotkey"] = shell.hotkey_text if shell.hotkey_registered else None
    if shell.error:
        result["shell_error"] = shell.error

    window = webview.create_window(
        "NovaMind Quick", html=_HTML, width=380, height=240,
        frameless=True, on_top=True, resizable=False,
    )
    result["window_created"] = window is not None
    threading.Timer(hold_seconds, window.destroy).start()
    start = time.perf_counter()
    webview.start()
    result["run_seconds"] = round(time.perf_counter() - start, 3)
    shell.stop()
    result["rss_mb"] = _rss_mb()
    result["rss_after_mb"] = _rss_mb()
    return result


def run_pyside_selftest(hold_seconds: float = 1.2) -> dict:
    import_seconds, error = _import_seconds("PySide6.QtWidgets")
    result: dict = {
        "mode": "pyside", "available": error is None,
        "import_seconds": import_seconds,
    }
    if error:
        result["error"] = error
        return result

    from PySide6 import QtCore, QtWidgets

    start = time.perf_counter()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    icon = app.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
    tray = QtWidgets.QSystemTrayIcon(icon, app)
    tray.setToolTip("NovaMind Spike")
    tray.show()
    window = QtWidgets.QWidget(None, QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
    window.resize(380, 240)
    window.show()
    result["init_seconds"] = round(time.perf_counter() - start, 3)
    result["tray_available"] = bool(QtWidgets.QSystemTrayIcon.isSystemTrayAvailable())
    result["window_created"] = window.isVisible()
    QtCore.QTimer.singleShot(int(hold_seconds * 1000), app.quit)
    start = time.perf_counter()
    app.exec()
    result["run_seconds"] = round(time.perf_counter() - start, 3)
    tray.hide()
    result["rss_mb"] = _rss_mb()
    return result


def run_smoke(port: int = 8877) -> dict:
    """A0 门禁冒烟：单实例、本机守卫、托盘/热键、后端启停与端口释放。"""
    from .single_instance import SingleInstance

    report: dict = {"mode": "smoke"}

    # 1) 单实例
    first = SingleInstance("Local\\NovaMind.A0.Smoke")
    second = SingleInstance("Local\\NovaMind.A0.Smoke")
    report["single_instance_first"] = first.acquire()
    report["single_instance_second_rejected"] = not second.acquire()
    first.release()
    report["single_instance_after_release"] = second.acquire()
    second.release()

    # 2) 本机 API 守卫 + 后端生命周期
    from novamind.webui.app import _start_server, _wait_for_server
    from novamind.webui.local_guard import SESSION_COOKIE, session_token

    import urllib.error
    import urllib.request

    server = _start_server("127.0.0.1", port, local_guard=True)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(base, timeout=15)
        report["health_ok"] = _status(base + "/health") == 200

        # Host 伪造 → 403
        try:
            urllib.request.urlopen(
                urllib.request.Request(base + "/health", headers={"Host": "evil.example"}),
                timeout=5,
            )
            report["host_spoof_blocked"] = False
        except urllib.error.HTTPError as exc:
            report["host_spoof_blocked"] = exc.code == 403

        # 无 Cookie 的写请求 → 403
        try:
            urllib.request.urlopen(
                urllib.request.Request(base + "/sessions/none", method="DELETE"), timeout=5
            )
            report["csrf_blocked"] = False
        except urllib.error.HTTPError as exc:
            report["csrf_blocked"] = exc.code == 403

        # 首页下发 Cookie；带 Cookie 的写请求通过守卫（幂等删除返回 200）
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            set_cookie = resp.headers.get("Set-Cookie", "")
        report["cookie_issued"] = SESSION_COOKIE in set_cookie
        cookie = f"{SESSION_COOKIE}={session_token()}"
        try:
            req = urllib.request.Request(
                base + "/sessions/none", method="DELETE",
                headers={"Cookie": cookie},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                report["authorized_delete"] = resp.status == 200
        except urllib.error.HTTPError as exc:
            report["authorized_delete"] = False
            report["authorized_delete_status"] = exc.code
    finally:
        server.should_exit = True
        deadline = time.time() + 10
        while time.time() < deadline and _port_open("127.0.0.1", port):
            time.sleep(0.1)
        report["port_released"] = not _port_open("127.0.0.1", port)

    # 3) 托盘 + 全局热键（无窗口，独立薄壳）
    from .win32_shell import Win32Shell

    shell = Win32Shell(on_open=lambda: None)
    report["shell_started"] = shell.start()
    report["tray_added"] = shell.tray_added
    report["hotkey_registered"] = shell.hotkey_registered
    report["shell_error"] = shell.error
    shell.stop()
    return report


def _status(url: str) -> int:
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NovaMind A0 desktop spike")
    parser.add_argument("--mode", choices=["pywebview", "pyside"], default="pywebview")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()

    if args.smoke:
        report = run_smoke(args.port)
    elif args.mode == "pyside":
        report = run_pyside_selftest()
    else:
        report = run_pywebview_selftest()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = (
        report.get("available") is False
        or report.get("shell_error")
        or report.get("error")
        or report.get("tray_added") is False
        or report.get("hotkey_registered") is False
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
