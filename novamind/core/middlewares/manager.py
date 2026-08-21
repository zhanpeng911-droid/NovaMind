"""MiddlewareManager — 中间件注册与钩子分发。

按注册顺序依次调用所有中间件的同一钩子，合并 MiddlewareResult。
所有分发方法均 async（优先调 async 钩子，缺省回退同步钩子）。
"""

from __future__ import annotations

from typing import Iterable

from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult


class MiddlewareManager:
    def __init__(self, middlewares: Iterable[BaseAgentMiddleware] | None = None) -> None:
        self._middlewares: list[BaseAgentMiddleware] = list(middlewares or [])

    @property
    def middlewares(self) -> list[BaseAgentMiddleware]:
        return list(self._middlewares)

    def add(self, middleware: BaseAgentMiddleware) -> None:
        self._middlewares.append(middleware)

    def add_all(self, middlewares: Iterable[BaseAgentMiddleware]) -> None:
        self._middlewares.extend(middlewares)

    # ── 分发 ────────────────────────────────────────────────────────

    async def dispatch(self, hook: str, ctx: MiddlewareContext) -> MiddlewareResult:
        """按顺序调用所有中间件的指定钩子，合并结果。

        hook 取值：before_agent / after_agent / before_model / after_model / wrap_tool_call
        """
        merged = MiddlewareResult()
        for mw in self._middlewares:
            result = await self._invoke_hook(mw, hook, ctx)
            if result is not None:
                merged = _merge(merged, result)
        return merged

    async def _invoke_hook(self, mw: BaseAgentMiddleware, hook: str, ctx: MiddlewareContext):
        """优先调 async 钩子，缺省回退同步钩子。

        仅当子类在自身 __dict__ 中定义了钩子时才调用（避免命中基类 no-op）。
        """
        cls = type(mw)
        async_name = f"a{hook}"
        if async_name in cls.__dict__:
            return await getattr(mw, async_name)(ctx)
        if hook in cls.__dict__:
            return getattr(mw, hook)(ctx)
        return None


def _merge(a: MiddlewareResult, b: MiddlewareResult) -> MiddlewareResult:
    """合并两个 MiddlewareResult：messages_patch 拼接，state_patch 浅合并，override 后者覆盖。"""
    state_patch = None
    if a.state_patch is not None or b.state_patch is not None:
        state_patch = dict(a.state_patch or {})
        state_patch.update(b.state_patch or {})

    messages_patch = None
    if a.messages_patch is not None or b.messages_patch is not None:
        messages_patch = list(a.messages_patch or []) + list(b.messages_patch or [])

    override = b.override if b.override is not None else a.override

    return MiddlewareResult(
        state_patch=state_patch,
        messages_patch=messages_patch,
        override=override,
    )
