"""MemoryRecallMiddleware — before_model 召回注入 + after_model 清除（L4）。

吸收 Poirot `middlewares/memory_recall_middleware.py`：
- 注入方式：per-call HumanMessage（hide_from_ui=True，不进 system prompt cache）
- set_turn_id 注入/清除（traceability）
- recalled_memories 只存索引（id+score+strength），不存全量内容
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage

from ..memory.strategies.default.manager import set_turn_id
from ..memory.types import MemoryQuery, RetrievalResult
from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4


class MemoryRecallMiddleware(BaseAgentMiddleware):
    """记忆召回中间件（before_model recall + after_model 清除 turn_id）。"""

    def __init__(
        self,
        memory_provider: Any,
        *,
        enable_recall: bool = True,
        enable_extract: bool = False,
        token_budget: int = 2000,
    ) -> None:
        self._provider = memory_provider
        self._enable_recall = enable_recall
        self._enable_extract = enable_extract
        self._token_budget = token_budget

    async def abefore_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        if not self._enable_recall:
            return None

        query = self._extract_query(ctx)
        if not query:
            return None

        turn_id = self._build_turn_id(ctx)
        set_turn_id(turn_id)

        results = self._provider.retriever().retrieve(MemoryQuery(text=query))
        if not results:
            set_turn_id(None)
            return None

        memories_text = self._format_recall(results, self._token_budget)
        return MiddlewareResult(
            messages_patch=[
                HumanMessage(
                    content=memories_text,
                    name="memory_recall",
                    additional_kwargs={"hide_from_ui": True},
                )
            ],
            state_patch={
                "metadata": {
                    "recalled_memories": [
                        {"id": r.trace.id, "score": r.score, "strength": r.strength}
                        for r in results
                    ]
                }
            },
        )

    async def aafter_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        set_turn_id(None)
        return None

    def _extract_query(self, ctx: MiddlewareContext) -> str:
        messages = ctx.messages or []
        if not messages and ctx.state is not None:
            messages = getattr(ctx.state, "messages", []) or []
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage) and msg.name != "memory_recall":
                content = msg.content
                if isinstance(content, str):
                    return content
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict) and "text" in first:
                        return str(first["text"])
                return str(content)
        return ""

    def _format_recall(self, results: list[RetrievalResult], token_budget: int) -> str:
        max_chars = token_budget * _CHARS_PER_TOKEN
        lines: list[str] = ["[Recalled Memories]"]
        current_len = len(lines[0])
        for r in results:
            line = f"[score={r.score:.2f} strength={r.strength:.2f}] {r.trace.content}"
            if current_len + len(line) + 1 > max_chars:
                break
            lines.append(line)
            current_len += len(line) + 1
        return "\n".join(lines)

    def _build_turn_id(self, ctx: MiddlewareContext) -> str:
        messages = ctx.messages or []
        if not messages and ctx.state is not None:
            messages = getattr(ctx.state, "messages", []) or []
        thread_id = ctx.thread_id or "unknown"
        return f"{thread_id}:turn:{len(messages)}"
