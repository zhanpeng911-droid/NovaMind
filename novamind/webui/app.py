"""NovaMind WebUI 桌面窗口（pywebview）。

把 FastAPI 后端跑在后台线程，pywebview 在前台开一个桌面窗口加载本地页面。
支持端口自动重试 + 无 GUI 环境降级到浏览器。
"""

from __future__ import annotations

import threading
import time
import urllib.request
import webbrowser

import uvicorn
import webview


def _wait_for_server(url: str, timeout: float = 5.0) -> None:
    """轮询 /health 直到后端就绪，超时抛 TimeoutError。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(f"NovaMind 后端在 {timeout}s 内未就绪")


def _start_server(host: str, port: int):
    """后台线程启动 uvicorn，返回 server（可设 should_exit 停止）。"""
    from novamind.webui.server import app

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    return server


def _find_available_url(host: str, port: int, max_tries: int = 10):
    """端口冲突时自动顺延，返回 (server, url)。"""
    for offset in range(max_tries):
        candidate_port = port + offset
        server = _start_server(host, candidate_port)
        url = f"http://{host}:{candidate_port}"
        try:
            _wait_for_server(url, timeout=3.0)
            return server, url
        except TimeoutError:
            server.should_exit = True
    raise RuntimeError(f"无法在端口 {port}~{port + max_tries - 1} 启动后端，请检查端口占用")


def run_gui(host: str = "127.0.0.1", port: int = 8765, debug: bool = False) -> None:
    """启动桌面窗口：后台 uvicorn + 前台 pywebview，失败降级到浏览器。"""
    server, url = _find_available_url(host, port)

    try:
        webview.create_window(
            "NovaMind",
            url,
            width=1000,
            height=720,
            min_size=(640, 480),
        )
        webview.start(debug=debug)
    except Exception:
        # 无图形环境（如 headless）降级到系统浏览器
        print(f"[提示] pywebview 窗口无法启动，已降级到浏览器：{url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        server.should_exit = True
