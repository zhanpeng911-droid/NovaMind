"""L5 bounded user-message batches, independent of context-window pruning.

Capture before governance prunes history. Count each user turn once, not each
tool/model iteration. Automatic output remains proposed until user approval.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage

from ..memory.worker import MemoryTask, MemoryWorker
from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult

logger = logging.getLogger(__name__)


class MemoryConsolidationMiddleware(BaseAgentMiddleware):
    """自动沉淀中间件：aafter_model 每 N 轮丢任务到 worker。"""

    def __init__(self, worker: MemoryWorker, *, trigger_every_n_turns: int = 10) -> None:
        self._worker = worker
        self._n = max(1, trigger_every_n_turns)

    async def abefore_agent(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        self._collect(ctx)
        return None

    def _collect(self, ctx: MiddlewareContext) -> None:
        from ..memory.config import get_memory_config
        from ..personal import memory_allowed, request_context

        if ctx.state is None:
            return
        metadata = ctx.state.metadata
        if not get_memory_config().enable_auto_extract:
            metadata.pop("memory_extract_batch", None)
            return
        if not memory_allowed(write=True):
            return
        messages = ctx.messages or getattr(ctx.state, "messages", []) or []
        last = next((m for m in reversed(messages) if getattr(m, "type", "") == "human"
                     and not getattr(m, "name", None)), None)
        # Images/attachments and model/tool content are not long-term evidence.
        if last is None or not isinstance(last.content, str) or not last.content.strip():
            return
        key = str(last.id or request_context.get().get("turn_id") or id(last))
        if metadata.get("memory_collected_turn") == key:
            return
        scope = request_context.get().get("scope", "global")
        epoch = self._worker._governance.store.epoch() if self._worker._governance else 0
        batch = metadata.get("memory_extract_batch", {})
        if batch.get("scope") != scope or batch.get("epoch") != epoch:
            batch = {"scope": scope, "epoch": epoch, "messages": []}
        entries = [*batch.get("messages", []), {"id": key, "text": last.content[:4000]}][-self._n:]
        metadata["memory_extract_batch"] = {**batch, "messages": entries}
        metadata["memory_collected_turn"] = key
        metadata["memory_user_turn_count"] = int(metadata.get("memory_user_turn_count", 0)) + 1

    async def aafter_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        from ..memory.config import get_memory_config

        if not get_memory_config().enable_auto_extract:
            return None
        from ..personal import memory_allowed, request_context
        if not memory_allowed(write=True):
            return None
        if ctx.state is not None:
            self._collect(ctx)
            metadata = ctx.state.metadata
            batch = metadata.get("memory_extract_batch", {})
            entries = batch.get("messages", [])
            if len(entries) < self._n:
                return None
            recent = [HumanMessage(content=entry["text"], id=entry["id"]) for entry in entries]
            user_turn_count = metadata.get("memory_user_turn_count", len(entries))
            task = MemoryTask(
                thread_id=ctx.thread_id or "unknown", messages=recent,
                turn_count=user_turn_count, epoch=batch["epoch"], scope=batch["scope"],
            )
            self._worker.submit(task)
            metadata.pop("memory_extract_batch", None)
            return None
        # Support standalone hook callers without an AgentState as before.
        messages = ctx.messages or []
        messages = [m for m in messages if getattr(m, "type", None) == "human"
                    and not getattr(m, "name", None)]
        user_turn_count = len(messages)
        if user_turn_count == 0 or user_turn_count % self._n != 0:
            return None

        recent = [m for m in messages[-self._n:] if isinstance(m.content, str)]
        if not recent:
            # A pasted image is not permission to make its contents a long-term fact.
            return None
        task = MemoryTask(
            thread_id=ctx.thread_id or "unknown",
            messages=recent,
            turn_count=user_turn_count,
            epoch=self._worker._governance.store.epoch() if self._worker._governance else 0,
            scope=request_context.get().get("scope", "global"),
        )
        self._worker.submit(task)
        logger.debug("MemoryConsolidationMiddleware submitted task: thread=%s turn=%d", ctx.thread_id, user_turn_count)
        return None
