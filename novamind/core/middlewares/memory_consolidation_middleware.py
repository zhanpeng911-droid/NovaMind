"""MemoryConsolidationMiddleware — aafter_model 每 N 轮非阻塞触发沉淀（L5）。

吸收 Poirot `middlewares/memory_consolidation_middleware.py`：
- 仅 aafter_model 触发，turn_count % N == 0 才 submit
- submit 后立即返回（worker 异步处理，不阻塞主循环）
- messages 截断最近 N*2 条（避免 token 爆炸）
"""

from __future__ import annotations

import logging


from ..memory.worker import MemoryTask, MemoryWorker
from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult

logger = logging.getLogger(__name__)


class MemoryConsolidationMiddleware(BaseAgentMiddleware):
    """自动沉淀中间件：aafter_model 每 N 轮丢任务到 worker。"""

    def __init__(self, worker: MemoryWorker, *, trigger_every_n_turns: int = 10) -> None:
        self._worker = worker
        self._n = max(1, trigger_every_n_turns)

    async def aafter_model(self, ctx: MiddlewareContext) -> MiddlewareResult | None:
        messages = ctx.messages or []
        if not messages and ctx.state is not None:
            messages = getattr(ctx.state, "messages", []) or []

        user_turn_count = sum(1 for m in messages if getattr(m, "type", None) == "human" and m.name != "memory_recall")
        if user_turn_count == 0 or user_turn_count % self._n != 0:
            return None

        recent = messages[-(self._n * 2):]
        task = MemoryTask(
            thread_id=ctx.thread_id or "unknown",
            messages=recent,
            turn_count=user_turn_count,
        )
        self._worker.submit(task)
        logger.debug("MemoryConsolidationMiddleware submitted task: thread=%s turn=%d", ctx.thread_id, user_turn_count)
        return None
