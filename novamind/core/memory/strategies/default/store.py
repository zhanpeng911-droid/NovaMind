"""MarkdownFileStore — Markdown 持久化 truth source。

吸收 Poirot `memory/strategies/default/store.py`：
- Markdown-as-Truth：traces.md 是 truth source，内存索引是 derived（可重建）
- 单文件 + `<!-- trace: {id} -->` 分隔符
- 文件锁 threading.Lock 保护写（单进程）
- 解析容错：frontmatter 损坏 log + 跳过，不崩
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from ...exceptions import MemoryConflictError, MemoryNotFoundError
from ...schema import Association, MemoryTrace, MemoryType, OperationLog
from ...types import MemoryFilter

logger = logging.getLogger(__name__)

_TRACE_SEPARATOR = re.compile(
    r"<!-- trace: ([a-f0-9]+) -->\n(.*?)(?=<!-- trace:|$)", re.DOTALL
)


class MarkdownFileStore:
    """Markdown 文件持久化。单文件 traces.md + 内存索引。"""

    def __init__(self, storage_path: str | Path) -> None:
        self._root = self._resolve_storage_path(storage_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self._traces_file = self._root / "traces.md"
        self._lock = threading.Lock()
        self._traces: dict[str, MemoryTrace] = {}
        self._load()

    def _resolve_storage_path(self, storage_path: str | Path) -> Path:
        p = Path(storage_path)
        if p.is_absolute():
            return p
        return p.resolve()

    def _load(self) -> None:
        if not self._traces_file.exists():
            self._traces_file.write_text("# Memory Traces\n\n", encoding="utf-8")
            return
        content = self._traces_file.read_text(encoding="utf-8")
        for match in _TRACE_SEPARATOR.finditer(content):
            trace_id = match.group(1)
            trace_body = match.group(2).strip()
            trace = self._parse_trace(trace_id, trace_body)
            if trace is not None:
                self._traces[trace.id] = trace
        logger.info("MarkdownFileStore loaded %d traces from %s", len(self._traces), self._traces_file)

    def _parse_trace(self, trace_id: str, body: str) -> MemoryTrace | None:
        try:
            if not body.startswith("---\n"):
                logger.warning("trace %s missing frontmarker, skipped", trace_id)
                return None
            parts = body[4:].split("\n---\n", 1)
            if len(parts) != 2:
                logger.warning("trace %s malformed frontmatter, skipped", trace_id)
                return None
            frontmatter_text, content = parts
            data = yaml.safe_load(frontmatter_text)
            if not isinstance(data, dict):
                logger.warning("trace %s frontmatter not dict, skipped", trace_id)
                return None
            if "id" not in data or "type" not in data:
                logger.warning("trace %s missing id/type, skipped", trace_id)
                return None

            type_val = data.pop("type")
            mem_type = MemoryType(type_val) if not isinstance(type_val, MemoryType) else type_val

            assocs_data = data.pop("associations", [])
            associations = tuple(Association(**a) for a in assocs_data) if assocs_data else ()

            log_data = data.pop("operation_log", [])
            operation_log = tuple(OperationLog(**log) for log in log_data) if log_data else ()

            embedding = data.pop("embedding", None)
            if embedding is not None:
                embedding = tuple(embedding)

            return MemoryTrace(
                content=content,
                type=mem_type,
                associations=associations,
                operation_log=operation_log,
                embedding=embedding,
                **data,
            )
        except Exception as exc:
            logger.warning("Failed to parse trace %s: %s", trace_id, exc)
            return None

    def _serialize_trace(self, trace: MemoryTrace) -> str:
        data = {
            "id": trace.id,
            "type": trace.type.value,
            "strength": trace.strength,
            "base_strength": trace.base_strength,
            "decay_rate": trace.decay_rate,
            "access_count": trace.access_count,
            "last_accessed": trace.last_accessed,
            "importance": trace.importance,
            "associations": [
                {"target_id": a.target_id, "strength": a.strength, "type": a.type}
                for a in trace.associations
            ],
            "embedding": list(trace.embedding) if trace.embedding is not None else None,
            "source": trace.source,
            "created_at": trace.created_at,
            "metadata": trace.metadata,
            "operation_log": [
                {
                    "timestamp": log.timestamp,
                    "operation": log.operation,
                    "actor": log.actor,
                    "diff": self._diff_to_serializable(log.diff),
                }
                for log in trace.operation_log
            ],
        }
        frontmatter = yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False
        ).strip()
        return f"---\n{frontmatter}\n---\n{trace.content}"

    @staticmethod
    def _diff_to_serializable(diff: dict[str, Any] | None) -> dict[str, Any] | None:
        if diff is None:
            return None
        result: dict[str, Any] = {}
        for k, v in diff.items():
            if isinstance(v, tuple):
                result[k] = list(v)
            else:
                result[k] = v
        return result

    def add(self, trace: MemoryTrace) -> None:
        with self._lock:
            if trace.id in self._traces:
                raise MemoryConflictError(
                    f"trace already exists: {trace.id}", old_id=trace.id, new_id=trace.id
                )
            self._traces[trace.id] = trace
            self._append_to_file(trace)

    def get(self, trace_id: str) -> MemoryTrace | None:
        return self._traces.get(trace_id)

    def _append_to_file(self, trace: MemoryTrace) -> None:
        block = f"<!-- trace: {trace.id} -->\n{self._serialize_trace(trace)}\n\n"
        with open(self._traces_file, "a", encoding="utf-8") as f:
            f.write(block)

    def _rewrite_file(self) -> None:
        with open(self._traces_file, "w", encoding="utf-8") as f:
            f.write("# Memory Traces\n\n")
            for trace in self._traces.values():
                f.write(f"<!-- trace: {trace.id} -->\n{self._serialize_trace(trace)}\n\n")

    def update(self, trace: MemoryTrace) -> None:
        with self._lock:
            if trace.id not in self._traces:
                raise MemoryNotFoundError(trace.id)
            self._traces[trace.id] = trace
            self._rewrite_file()

    def batch_update(self, traces: list[MemoryTrace]) -> None:
        with self._lock:
            for trace in traces:
                if trace.id not in self._traces:
                    raise MemoryNotFoundError(trace.id)
            for trace in traces:
                self._traces[trace.id] = trace
            self._rewrite_file()

    def remove(self, trace_id: str) -> None:
        with self._lock:
            if trace_id in self._traces:
                del self._traces[trace_id]
                self._rewrite_file()

    def list_by_type(self, type: MemoryType) -> list[MemoryTrace]:
        type_key = type.value if isinstance(type, MemoryType) else str(type)
        return [t for t in self._traces.values() if t.type.value == type_key]

    def list_by_filter(self, filter: MemoryFilter) -> list[MemoryTrace]:
        result = list(self._traces.values())
        if filter.type_filter is not None:
            type_key = (
                filter.type_filter.value
                if isinstance(filter.type_filter, MemoryType)
                else str(filter.type_filter)
            )
            result = [t for t in result if t.type.value == type_key]
        if filter.max_age_hours is not None:
            now = time.time()
            max_age_seconds = filter.max_age_hours * 3600.0
            result = [
                t for t in result
                if (now - (t.last_accessed if t.last_accessed > 0 else t.created_at)) <= max_age_seconds
            ]
        if filter.metadata_filter:
            result = [
                t for t in result
                if all(t.metadata.get(k) == v for k, v in filter.metadata_filter.items())
            ]
        return result

    def list_all(self) -> list[MemoryTrace]:
        return list(self._traces.values())
