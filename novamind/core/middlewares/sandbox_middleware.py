"""SandboxMiddleware — Agent 生命周期内的 Sandbox 获取 / 绑定 / 释放。

Phase 2：把 Phase 1 的 Sandbox 上下文（ContextVar + provider-backed 工具工厂）
接到状态机的 run 生命周期上：

- abefore_agent：provider.acquire_async(thread_id) 获取 Sandbox，绑定到
  AgentState.sandbox 与当前执行上下文（ContextVar）；
- abefore_model / awrap_tool_call：防御性恢复 ContextVar（正常情况下
  abefore_agent 的绑定在同一条协程内一直有效；这里兜底防止中途被重置）；
- aafter_agent：恢复全部 ContextVar token、清空 state.sandbox 并 release
  沙箱——成功、异常、取消、generator 提前 close 各路径都恰好执行一次
  （release 以 pop 记录保证幂等；同步 release 直接调用，取消路径不会被
  取消打断，这也是默认 Local provider 的 no-op 语义所能承受的）。

SandboxMiddleware 必须排在其他中间件之前（见 create_agent_app 装配顺序），
使 OrchestrationMiddleware 的 delegate 能读到父 Sandbox 信息。
"""

from __future__ import annotations

import asyncio
import threading

from ..sandbox.context import (
    get_current_sandbox,
    reset_current_sandbox,
    set_current_sandbox,
)
from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult


class _ActiveRun:
    """一次 run 的沙箱绑定记录：sandbox_id + 需在释放时恢复的 token 栈。"""

    __slots__ = ("sandbox_id", "tokens")

    def __init__(self, sandbox_id: str, tokens: list):
        self.sandbox_id = sandbox_id
        self.tokens = tokens


class SandboxMiddleware(BaseAgentMiddleware):
    """按 thread 获取 Sandbox 并在 run 期间绑定到执行上下文。"""

    def __init__(self, provider, *, user_id: str | None = None):
        self._provider = provider
        self._user_id = user_id
        # key: id(state)。state 在 run 期间被 agent._states 持有，id 稳定；
        # 记录本身不引用 state，避免中间件延长状态生命周期。
        self._active: dict[int, _ActiveRun] = {}
        self._lock = threading.Lock()

    # ── agent 生命周期 ──────────────────────────────────────────────

    async def abefore_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        state = ctx.state
        if state is None:
            return None
        sandbox_id = await self._provider.acquire_async(
            ctx.thread_id, user_id=self._user_id
        )
        sandbox = self._provider.get(sandbox_id)
        if sandbox is None:
            raise RuntimeError(f"sandbox not found after acquire: {sandbox_id}")
        state.sandbox = sandbox
        token = set_current_sandbox(sandbox)
        with self._lock:
            self._active[id(state)] = _ActiveRun(sandbox_id, [token])
        return None

    async def aafter_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        state = ctx.state
        if state is None:
            return None
        with self._lock:
            run = self._active.pop(id(state), None)
        if run is None:
            return None  # 恰好一次：没有 acquire 过就没有 release
        # 同一条协程内 set 的 token 在此恢复（LIFO）；同步操作，
        # 取消/异常路径也不会被打断。
        for token in reversed(run.tokens):
            try:
                reset_current_sandbox(token)
            except ValueError:
                pass  # token 所属 context 已变化时的防御
        state.sandbox = None
        release = getattr(self._provider, "release", None)
        if release is not None:
            if asyncio.iscoroutinefunction(release):
                await release(run.sandbox_id)
            else:
                # 同步 release 直接调用（Local 为 no-op/纯内存操作），
                # 保证取消路径下也恰好执行一次；阻塞型 provider 的
                # 异步化留给后续 Phase 处理。
                release(run.sandbox_id)
        return None

    # ── model / 工具阶段：防御性恢复 ContextVar ─────────────────────

    async def abefore_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        self._rebind(ctx.state)
        return None

    async def awrap_tool_call(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        self._rebind(ctx.state)
        return None

    def _rebind(self, state) -> None:
        """当前上下文缺少（或不是）本 run 的 Sandbox 时重新绑定。

        token 记入该 run 的栈，aafter_agent 统一恢复。"""
        if state is None:
            return
        with self._lock:
            run = self._active.get(id(state))
        if run is None:
            return
        sandbox = getattr(state, "sandbox", None)
        if sandbox is not None and get_current_sandbox() is not sandbox:
            run.tokens.append(set_current_sandbox(sandbox))
