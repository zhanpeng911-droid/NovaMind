"""OrchestrationMiddleware — 多 Agent 横切：set_current_state + delegate 打点。

吸收 Poirot `multiagent/middleware.py`：拦截 delegate_to_* 工具调用，set_current_state
让 tool handler 读 ThreadState（共享沙箱透传），并打点。
"""

from __future__ import annotations


from ..multiagent.tools import set_current_state
from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult


def _state_to_dict(state) -> dict:
    """AgentState → dict（含 messages/sandbox/metadata）。"""
    if state is None:
        return {}
    if isinstance(state, dict):
        return state
    d = {}
    for attr in ("messages", "summary", "metadata", "sandbox"):
        if hasattr(state, attr):
            d[attr] = getattr(state, attr)
    return d


class OrchestrationMiddleware(BaseAgentMiddleware):
    """横切：wrap_tool_call 时 set_current_state，delegate_to_* 打点。"""

    def __init__(self, metrics_store=None) -> None:
        self._metrics = metrics_store

    async def awrap_tool_call(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        # 让 delegate tool handler 能读 ThreadState（sandbox_id 透传）
        state_dict = _state_to_dict(ctx.state)
        set_current_state(state_dict)

        tool_call = ctx.tool_call
        tool_name = ""
        if isinstance(tool_call, dict):
            tool_name = tool_call.get("name", "")
        elif hasattr(tool_call, "get"):
            tool_name = tool_call.get("name", "")

        if not tool_name.startswith("delegate_to_"):
            return None

        specialist_name = tool_name.removeprefix("delegate_to_")
        if self._metrics is not None:
            try:
                self._metrics.record_selection(specialist_name)
            except Exception:
                pass
        return None
