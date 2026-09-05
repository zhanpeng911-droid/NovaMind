"""Sandbox 运行时上下文（ContextVar）。

Phase 1：工具执行需要知道"当前这次 run 用哪个 Sandbox"。
用 ContextVar 携带，工具工厂从上下文取 Sandbox；缺少上下文时 fail closed。
asyncio.to_thread() 会复制调用上下文，因此同步 LangChain tool 的现有执行方式
（agent → to_thread → tool.invoke）能继续拿到同一 Sandbox。
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Any

_current_sandbox: contextvars.ContextVar["Any | None"] = contextvars.ContextVar(
    "novamind_current_sandbox", default=None
)


def get_current_sandbox() -> Any | None:
    """返回当前执行上下文中的 Sandbox；未设置时返回 None。"""
    return _current_sandbox.get()


def set_current_sandbox(sandbox: Any) -> contextvars.Token:
    """把 Sandbox 绑定到当前上下文，返回可恢复的 token。"""
    return _current_sandbox.set(sandbox)


def reset_current_sandbox(token: contextvars.Token) -> None:
    _current_sandbox.reset(token)


def require_current_sandbox() -> Any:
    """获取当前 Sandbox，缺失时抛 RuntimeError（fail closed）。"""
    sandbox = get_current_sandbox()
    if sandbox is None:
        raise RuntimeError("no active sandbox in this execution context")
    return sandbox


class SandboxContextManager:
    """异步上下文管理器：进入时把 provider 为该 thread 获取的 Sandbox 绑到上下文，
    退出时恢复。供 SandboxMiddleware 与测试直接使用。"""

    def __init__(self, provider: Any, thread_id: str, *, user_id: str | None = None):
        self._provider = provider
        self._thread_id = thread_id
        self._user_id = user_id
        self._token: contextvars.Token | None = None
        self._sandbox_id: str | None = None

    async def __aenter__(self) -> Any:
        if hasattr(self._provider, "acquire_async"):
            self._sandbox_id = await self._provider.acquire_async(
                self._thread_id, user_id=self._user_id
            )
        else:
            self._sandbox_id = await asyncio.to_thread(
                self._provider.acquire, self._thread_id, user_id=self._user_id
            )
        sandbox = self._provider.get(self._sandbox_id)
        if sandbox is None:
            raise RuntimeError(f"sandbox not found after acquire: {self._sandbox_id}")
        self._token = set_current_sandbox(sandbox)
        return sandbox

    async def __aexit__(self, *exc) -> None:
        if self._token is not None:
            reset_current_sandbox(self._token)
            self._token = None
        if self._sandbox_id is not None:
            release = getattr(self._provider, "release", None)
            if release is not None:
                if asyncio.iscoroutinefunction(release):
                    await release(self._sandbox_id)
                else:
                    await asyncio.to_thread(release, self._sandbox_id)
            self._sandbox_id = None


# 幂等保护：同一 provider 上多线程共享的 provider 不应在上下文退出时重复 shutdown。
_owner_lock = threading.Lock()
