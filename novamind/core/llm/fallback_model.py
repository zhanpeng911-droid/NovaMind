"""FallbackChatModel — 角色路由链 + 运行时故障降级。

吸收 Poirot `config/fallback_model.py`：包装一组 BaseChatModel（按角色优先级链），
调用时从当前活跃 provider 试起；遇瞬时 API 错误（限流/超时/连接/5xx）自动降级到下一个，
记忆活跃 provider 避免每轮重试主力。bind_tools 对链内每个 model 绑定。
"""

from __future__ import annotations

import threading
from typing import Any, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


def _should_fallback(exc: Exception) -> bool:
    """是否应降级到下一个 provider。

    降级：网络/超时/限流/5xx 服务端错误（瞬时，换 provider 可能恢复）。
    不降级：400/401/404 等客户端错误（换 provider 也会失败，应暴露给上层）。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    try:
        import openai

        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
            return True
        rate_limit = getattr(openai, "RateLimitError", None)
        if rate_limit is not None and isinstance(exc, rate_limit):
            return True
        status_err = getattr(openai, "APIStatusError", None)
        if status_err is not None and isinstance(exc, status_err):
            status = getattr(exc, "status_code", None)
            if status is not None and status >= 500:
                return True
    except ImportError:
        pass
    return False


class FallbackChatModel(BaseChatModel):
    """按链顺序调用，瞬时 API 失败降级到下一个，记忆活跃 provider。"""

    models: list[Any]  # BaseChatModel 或其 bind_tools 后的 RunnableBinding
    provider_names: list[str] = []
    _active: int = PrivateAttr(default=0)
    # Phase 3：保护 _active 的起始快照与成功更新；只在内存操作时持有，
    # 绝不跨模型网络调用持有（否则并发调用会被串行化）。
    _active_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _snapshot_active(self) -> int:
        with self._active_lock:
            return self._active

    def _store_active(self, idx: int) -> None:
        with self._active_lock:
            self._active = idx

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_exc: Exception | None = None
        n = len(self.models)
        start = self._snapshot_active()
        for offset in range(n):
            idx = (start + offset) % n
            try:
                # 用 invoke 兼容 BaseChatModel 与 bind_tools 后的 RunnableBinding
                ai: AIMessage = self.models[idx].invoke(messages, stop=stop, **kwargs)
                self._store_active(idx)
                return ChatResult(generations=[ChatGeneration(message=ai)])
            except Exception as exc:
                if not _should_fallback(exc):
                    raise
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_exc: Exception | None = None
        n = len(self.models)
        start = self._snapshot_active()
        for offset in range(n):
            idx = (start + offset) % n
            try:
                ai: AIMessage = await self.models[idx].ainvoke(messages, stop=stop, **kwargs)
                self._store_active(idx)
                return ChatResult(generations=[ChatGeneration(message=ai)])
            except Exception as exc:
                if not _should_fallback(exc):
                    raise
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    @override
    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "FallbackChatModel":
        """对链内每个 model 绑定 tools，返回新的 FallbackChatModel（_active 重置）。"""
        bound = [m.bind_tools(tools, **kwargs) for m in self.models]
        return FallbackChatModel(models=bound, provider_names=list(self.provider_names))

    @property
    @override
    def _llm_type(self) -> str:
        return "fallback-chat-model"

    @property
    @override
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "providers": self.provider_names or [f"model-{i}" for i in range(len(self.models))],
            "active": self._active,
        }
