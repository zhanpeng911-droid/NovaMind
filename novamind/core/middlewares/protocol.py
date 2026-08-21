"""横切中间件协议（P0）。

吸收 Poirot 的 AgentMiddleware 钩子语义（before/after_agent、before/after_model、
wrap_tool_call），但适配 NovaMind 自研状态机——不依赖 LangGraph/LangChain 的 agent
middleware，用普通类 + 可覆盖钩子实现。

设计原则（对齐方案文档 §1.2）：
- 核心循环保持薄：横切关注点（记忆、技能、沙箱、上下文治理）都挂在这些钩子上
- 每个钩子默认 no-op，子类只覆盖需要的钩子
- 同步钩子 + async 钩子成对出现，状态机在 async 环境下优先调 async 版本
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MiddlewareContext:
    """横切中间件运行时上下文：跨钩子传递状态与运行时信息。"""

    state: Any = None                                   # AgentState 实例
    thread_id: str = "default"
    runtime: dict[str, Any] = field(default_factory=dict)  # run_id / model / journal / output_dir ...
    messages: list = field(default_factory=list)        # before/after_model 关注的当前消息列表
    tool_call: Any = None                               # wrap_tool_call 关注的工具调用请求


@dataclass
class MiddlewareResult:
    """钩子返回值：可选的状态补丁，由 MiddlewareManager 合并后应用。

    - state_patch：合并进 state 的字典更新（如 governance、summary、metadata）
    - messages_patch：本轮追加的消息（before_model 注入、after_model 收尾提示等）
    - override：wrap_tool_call 返回的工具结果覆盖（None 表示不覆盖）
    """

    state_patch: dict[str, Any] | None = None
    messages_patch: list | None = None
    override: Any = None

    def is_empty(self) -> bool:
        return (
            self.state_patch is None
            and self.messages_patch is None
            and self.override is None
        )


class BaseAgentMiddleware:
    """横切中间件基类：所有钩子默认 no-op，子类按需覆盖。"""

    # ── agent 生命周期 ──────────────────────────────────────────────
    def before_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    async def abefore_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    def after_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    async def aafter_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    # ── model 生命周期 ──────────────────────────────────────────────
    def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    async def abefore_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    def after_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    async def aafter_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    # ── 工具调用 ────────────────────────────────────────────────────
    def wrap_tool_call(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None

    async def awrap_tool_call(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        return None
