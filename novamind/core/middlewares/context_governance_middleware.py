"""ContextGovernanceMiddleware — 把 DefaultStrategy 挂到 before/after_model 等钩子。

吸收 Poirot 的 StrategyMiddleware adapter 思路：构造 GovernanceContext 调策略，
把 GovernanceResult 转 MiddlewareResult。governance 状态持久化在 state.metadata["governance"]。
"""

from __future__ import annotations

import logging

from ..context_engineering.contract import GovernanceContext
from ..context_engineering.strategies.default.strategy import DefaultStrategy
from ..context_engineering.utilities import token_counter
from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult

logger = logging.getLogger(__name__)


class ContextGovernanceMiddleware(BaseAgentMiddleware):
    """上下文治理中间件：token 预算追踪 + P0-P5 分段舍弃。"""

    def __init__(self, strategy: DefaultStrategy) -> None:
        self._strategy = strategy

    async def abefore_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        result = self._strategy.before_agent(self._make_ctx(ctx, "before_agent"))
        return self._to_middleware_result(result)

    async def abefore_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        result = self._strategy.before_model(self._make_ctx(ctx, "before_model"))
        return self._to_middleware_result(result)

    async def aafter_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        result = self._strategy.after_model(self._make_ctx(ctx, "after_model"))
        return self._to_middleware_result(result)

    async def aafter_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        result = self._strategy.after_agent(self._make_ctx(ctx, "after_agent"))
        return self._to_middleware_result(result)

    async def awrap_tool_call(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        result = self._strategy.wrap_tool_call(self._make_ctx(ctx, "wrap_tool_call"))
        return self._to_middleware_result(result)

    def _make_ctx(self, ctx: MiddlewareContext, hook: str) -> GovernanceContext:
        governance = None
        if ctx.state is not None:
            governance = getattr(ctx.state, "metadata", {}).get("governance")
        runtime = ctx.runtime or {}
        config = {"window": runtime.get("window", 0)}
        return GovernanceContext(
            state=ctx.state if ctx.state is not None else {},
            governance=governance,
            config=config,
            token_counter=token_counter,
            runtime=runtime,
            hook=hook,
            messages=ctx.messages,
            tool_result=ctx.tool_call,
        )

    @staticmethod
    def _to_middleware_result(result) -> MiddlewareResult | None:
        if result is None:
            return None
        state_patch = None
        if result.state_patch:
            state_patch = {}
            for k, v in result.state_patch.items():
                if k == "governance":
                    state_patch.setdefault("metadata", {})["governance"] = v
                else:
                    state_patch[k] = v
        return MiddlewareResult(
            state_patch=state_patch,
            messages_patch=result.messages_patch,
            override=result.override,
        )
