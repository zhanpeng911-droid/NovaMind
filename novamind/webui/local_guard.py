"""本机 API 安全守卫（A0）。

目标：loopback 不等于安全。恶意网页可以跨站请求 127.0.0.1 的服务，
因此需要：
1. Host 白名单（防 DNS rebinding）；
2. Origin 白名单（有 Origin 时必须 loopback）；
3. 本机会话令牌（Cookie 或 X-NovaMind-Token），拦截跨站写请求。

实现为纯 ASGI 中间件（不缓冲响应），保证 /chat 的 SSE 流式不受影响。
默认关闭，仅在桌面 Shell 启动时设置 NOVAMIND_LOCAL_GUARD=1 启用；
单元测试与既有 TestClient 用例不受影响。
"""

from __future__ import annotations

import os
import secrets
from urllib.parse import urlsplit

SESSION_COOKIE = "novamind_session"
_TOKEN = secrets.token_urlsafe(32)
_ENABLED: bool | None = None

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def set_local_guard_enabled(enabled: bool | None) -> None:
    """显式开关（桌面入口用）；None 表示回退到环境变量。"""
    global _ENABLED
    _ENABLED = enabled


def local_guard_enabled() -> bool:
    if _ENABLED is not None:
        return _ENABLED
    return os.getenv("NOVAMIND_LOCAL_GUARD", "0") == "1"


def session_token() -> str:
    return _TOKEN


def _host_name(host_header: str) -> str:
    """从 Host/Origin netloc 提取主机名（支持 IPv6 方括号与端口）。"""
    value = (host_header or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    return value.rsplit(":", 1)[0] if ":" in value else value


def host_allowed(host_header: str) -> bool:
    return _host_name(host_header) in _LOOPBACK_HOSTS


def origin_allowed(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return host_allowed(parts.netloc)


def _parse_cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


async def _reject(send, detail: str) -> None:
    body = ('{"detail":"%s"}' % detail).encode()
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class LocalGuardMiddleware:
    """纯 ASGI 守卫：Host/Origin/会话令牌三件套。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not local_guard_enabled():
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        if not host_allowed(headers.get("host", "")):
            await _reject(send, "forbidden host")
            return

        origin = headers.get("origin")
        if origin and not origin_allowed(origin):
            await _reject(send, "forbidden origin")
            return

        method = scope.get("method", "GET").upper()
        cookies = _parse_cookies(headers.get("cookie", ""))
        if method not in _SAFE_METHODS:
            token = headers.get("x-novamind-token") or cookies.get(SESSION_COOKIE)
            if token != _TOKEN:
                await _reject(send, "invalid local session token")
                return

        async def send_with_cookie(message):
            if (
                message.get("type") == "http.response.start"
                and method in _SAFE_METHODS
                # A persistent WebView can retain the previous process token.
                # Refresh only on an already host/origin-validated read request;
                # stale/missing tokens still cannot authorize any write.
                and cookies.get(SESSION_COOKIE) != _TOKEN
            ):
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"set-cookie",
                     f"{SESSION_COOKIE}={_TOKEN}; Path=/; SameSite=Strict".encode())
                ]
            await send(message)

        await self.app(scope, receive, send_with_cookie)
