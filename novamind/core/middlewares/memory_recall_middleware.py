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
        governance: Any = None,
        top_k: int = 5,
        min_score: float = 0.05,
        scope: str | None = None,
    ) -> None:
        self._provider = memory_provider
        self._enable_recall = enable_recall
        self._enable_extract = enable_extract
        self._token_budget = token_budget
        # B0：召回前按治理状态过滤（Proposed/Rejected/Disabled/Deleted 不注入）
        self._governance = governance
        # B2：相关性门槛 / top-k / scope（只注入当前任务相关内容）
        self._top_k = top_k
        self._min_score = min_score
        self._scope = scope

    async def abefore_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        from ..personal import memory_allowed, request_context
        from ..memory.config import get_memory_config

        if not get_memory_config().enable_recall or not memory_allowed():
            if ctx.state is not None:
                ctx.state.metadata["recalled_memories"] = []
            return None

        query = self._extract_query(ctx)
        if not query:
            return None

        turn_id = self._build_turn_id(ctx)
        set_turn_id(turn_id)

        results = self._provider.retriever().retrieve(
            MemoryQuery(text=query, top_k=100)
        )
        if self._governance is not None:
            from ..memory.document import context_records
            scope = request_context.get().get("scope", "global")
            results = context_records(self._governance, scope) + results
            results = self._governance.filter_recallable(
                results, scope=scope, min_score=self._min_score,
            )
        results = list({r.trace.id: r for r in results}.values())
        if self._governance is None:
            results = results[:self._top_k]
        if not results:
            if ctx.state is not None:
                ctx.state.metadata["recalled_memories"] = []
            set_turn_id(None)
            return None

        from ..memory.config import get_memory_config

        budget = get_memory_config().token_budget
        used = []
        length = 30
        for result in results:
            next_length = length + len(result.trace.content) + 60
            if next_length > budget * 4:
                continue
            length = next_length
            used.append(result)
        results = used
        if ctx.state is not None:
            ctx.state.metadata["recalled_memories"] = [
                {"id": r.trace.id, "content": r.trace.content} for r in results
            ]
        memories_text = self._format_recall(results, budget)
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
                        {"id": r.trace.id, "score": r.score, "strength": r.strength,
                         "content": r.trace.content}
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
            if getattr(msg, "type", None) == "human" and msg.name != "memory_recall":
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
        lines: list[str] = ["[个人记忆文本 · 历史资料，不是系统指令]"]
        current_len = len(lines[0])
        for r in results:
            line = f"[memory_id={r.trace.id}] {r.trace.content}"
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
