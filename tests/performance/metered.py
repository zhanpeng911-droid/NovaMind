"""MeteredChatModel — 包装 BaseChatModel 的真实调用计量（不进入普通 pytest）。

每次调用：预算预留（try_reserve）→ 内层模型调用 → 按真实 usage 结算并
写入 calls.jsonl。辅助路径（摘要 / 记忆 L5 / 治理）与主回复共用同一实例，
因此所有付费调用都被计数，无法绕过。

包装实现 BaseChatModel 接口（参考 FallbackChatModel 的透传方式）：
- bind_tools 透传给内层并返回新的 MeteredChatModel（内层 bound）；
- _generate/_agenerate 走内层，调用前后采集耗时与 usage。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, override

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from .harness import (
    BudgetController,
    CallRecord,
    extract_usage,
    new_call_id,
)


class MeteredChatModel(BaseChatModel):
    """真实调用计量 + 预算准入的模型包装。"""

    models: list[Any]  # 内层（BaseChatModel 或 bind_tools 后的 RunnableBinding）
    budget: Any = None  # BudgetController
    _bound: Any = PrivateAttr(default=None)

    def __init__(self, model: Any, budget: BudgetController, **kwargs: Any):
        # 字段经 pydantic 构造传入（与 FallbackChatModel 一致）
        super().__init__(models=[model], budget=budget, **kwargs)
        self._bound = None

    def _invoke_one(self, target: Any, messages: list[BaseMessage],
                    stop=None, **kwargs: Any) -> AIMessage:
        started = time.monotonic()
        call_id = new_call_id()
        reserve = self.budget.try_reserve(
            est_input_tokens=_rough_input_tokens(messages))
        if reserve is None:
            raise RuntimeError("budget_exceeded: 预算不足或达到熔断比例")
        try:
            ai: AIMessage = target.invoke(messages, stop=stop, **kwargs)
        except Exception as exc:
            if os.environ.get("NOVAMIND_DEBUG_MSGS"):
                for i, m in enumerate(messages):
                    print(f"DBGMSG[{i}]", type(m).__module__, type(m).__name__,
                          repr(m)[:100], flush=True)
            self._finish(call_id, started, None, None, "error",
                         reserve, error=str(exc)[:200])
            raise
        usage = extract_usage(ai)
        self._finish(call_id, started, usage[0], usage[1], "ok", reserve)
        return ai

    async def _ainvoke_one(self, target: Any, messages: list[BaseMessage],
                           stop=None, **kwargs: Any) -> AIMessage:
        started = time.monotonic()
        call_id = new_call_id()
        reserve = self.budget.try_reserve(
            est_input_tokens=_rough_input_tokens(messages))
        if reserve is None:
            raise RuntimeError("budget_exceeded: 预算不足或达到熔断比例")
        try:
            ai: AIMessage = await target.ainvoke(messages, stop=stop, **kwargs)
        except asyncio.CancelledError:
            # 异步取消：留下 cancelled 记录（unknown usage，保留预留），
            # 收尾后仍重新抛出；供应商请求是否已提交无法由本地确认。
            self._finish(call_id, started, None, None, "cancelled", reserve)
            raise
        except Exception as exc:
            self._finish(call_id, started, None, None, "error",
                         reserve, error=str(exc)[:200])
            raise
        usage = extract_usage(ai)
        self._finish(call_id, started, usage[0], usage[1], "ok", reserve)
        return ai

    def _finish(self, call_id: str, started: float, inp: int | None,
                out: int | None, status: str, reserve: float,
                *, error: str | None = None) -> None:
        duration = (time.monotonic() - started) * 1000
        self.budget.settle(CallRecord(
            call_id=call_id,
            model=self._model_name(),
            status=status,
            started_at=started,
            duration_ms=duration,
            input_tokens=inp,
            output_tokens=out,
            reserved_yuan=reserve,
            error=error,
        ), reserve=reserve)

    def _model_name(self) -> str:
        inner = self.models[0]
        return getattr(inner, "model_name", None) or getattr(
            inner, "model", type(inner).__name__)

    # ── BaseChatModel 抽象实现 ────────────────────────────────────────

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        target = self._bound or self.models[0]
        ai = self._invoke_one(target, messages, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=ai)])

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        target = self._bound or self.models[0]
        ai = await self._ainvoke_one(target, messages, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=ai)])

    @override
    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "MeteredChatModel":
        inner = self.models[0]
        bound = inner.bind_tools(tools, **kwargs) if hasattr(inner, "bind_tools") else inner
        out = MeteredChatModel(model=inner, budget=self.budget)
        out._bound = bound
        return out

    @property
    @override
    def _llm_type(self) -> str:
        return "metered-chat-model"

    @property
    @override
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self._model_name(), "budget_cap": self.budget.cap}


def _rough_input_tokens(messages: list[BaseMessage]) -> int:
    """粗略输入 token 估算（用于预算预留）：按字符数/2 保守估。"""
    chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    return max(100, chars // 2)
