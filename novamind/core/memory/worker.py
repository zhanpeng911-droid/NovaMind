"""MemoryWorker — 后台异步抽取 episodic + consolidate（L5）。

daemon 线程 + threading.Queue + LLM 构造注入（不反向依赖 app）。
aafter_model 非阻塞 submit 任务，worker 异步处理。
错误处理：LLM 失败 log + 跳过，不影响主流程。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any

from langchain_core.messages import BaseMessage

from .config import get_memory_config
from .schema import MemoryType
from .strategies.default.manager import DefaultMemoryManager, set_turn_id

logger = logging.getLogger(__name__)

_EXTRACT_PROMPT = """Analyze the following conversation and extract episodic memories worth remembering.
Return JSON array of {{"content": "...", "type": "episodic|semantic|procedural", "importance": 0.0-1.0}}.
Only extract noteworthy facts, decisions, or patterns. Skip trivial messages.
Return ONLY the JSON array, no preamble.

Conversation:
{conversation}
"""

_CONSOLEATE_PROMPT = """Merge the following {n} memories into one consolidated semantic knowledge.
Return only the merged content (no preamble, no JSON).

Memories:
{memories}
"""

_MAX_CONSOLIDATE = 10


@dataclass
class MemoryTask:
    thread_id: str
    messages: list[BaseMessage]
    turn_count: int


class MemoryWorker:
    """后台 worker：异步抽取 episodic + consolidate。"""

    def __init__(self, manager: DefaultMemoryManager, llm: Any) -> None:
        self._manager = manager
        self._llm = llm
        self._queue: Queue[MemoryTask] = Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="memory-worker", daemon=True)
        self._thread.start()

    def submit(self, task: MemoryTask) -> None:
        self._queue.put(task)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                self._process(task)
            except Exception as exc:
                logger.error(f"MemoryWorker task failed: {exc}", exc_info=True)

    def _process(self, task: MemoryTask) -> None:
        set_turn_id(f"worker:{task.thread_id}:{task.turn_count}")
        try:
            self._extract_and_encode(task)
            self._maybe_consolidate(task)
        finally:
            set_turn_id(None)

    def _extract_and_encode(self, task: MemoryTask) -> list[str]:
        conversation = self._format_messages(task.messages)
        prompt = _EXTRACT_PROMPT.format(conversation=conversation)
        try:
            response = self._llm.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            items = json.loads(content)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning(f"worker: LLM extract failed (not JSON): {exc}")
            return []

        if not isinstance(items, list):
            logger.warning(f"worker: LLM extract not list, got {type(items)}")
            return []

        ids: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                trace = self._manager.encode(
                    content=str(item["content"]),
                    type=MemoryType(item.get("type", "episodic")),
                    importance=float(item.get("importance", 0.5)),
                    source=f"worker:{task.thread_id}",
                )
                ids.append(trace.id)
            except Exception as exc:
                logger.warning(f"worker: encode failed for item {item}: {exc}")
        return ids

    def _maybe_consolidate(self, task: MemoryTask) -> None:
        config = get_memory_config()
        threshold = int(config.phase2.get("trigger_every_n_turns", 10))

        all_episodic = [
            t for t in self._manager._store.list_by_type(MemoryType.EPISODIC)
            if not t.metadata.get("forgotten")
        ]
        if len(all_episodic) < threshold:
            return

        all_episodic.sort(key=lambda t: t.created_at)
        to_consolidate = all_episodic[:_MAX_CONSOLIDATE]
        if len(to_consolidate) < 2:
            return

        memories_text = "\n".join(f"- {t.content}" for t in to_consolidate)
        prompt = _CONSOLEATE_PROMPT.format(n=len(to_consolidate), memories=memories_text)
        try:
            response = self._llm.invoke(prompt)
            merged = response.content if hasattr(response, "content") else str(response)
        except Exception as exc:
            logger.warning(f"worker: LLM consolidate failed: {exc}")
            return

        try:
            self._manager.consolidate([t.id for t in to_consolidate], merged)
            logger.info(f"worker: consolidated {len(to_consolidate)} episodic into 1 semantic")
        except Exception as exc:
            logger.warning(f"worker: consolidate failed: {exc}")

    @staticmethod
    def _format_messages(messages: list[BaseMessage]) -> str:
        lines: list[str] = []
        for m in messages:
            role = m.__class__.__name__
            content = m.content if isinstance(m.content, str) else str(m.content)
            lines.append(f"{role}: {content}")
        return "\n".join(lines)
