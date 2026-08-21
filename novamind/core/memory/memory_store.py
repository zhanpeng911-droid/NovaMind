"""MemoryStore Protocol — 记忆持久化契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .schema import MemoryTrace, MemoryType
from .types import MemoryFilter


@runtime_checkable
class MemoryStore(Protocol):
    """记忆持久化协议。默认实现 MarkdownFileStore。"""

    def add(self, trace: MemoryTrace) -> None: ...

    def get(self, trace_id: str) -> MemoryTrace | None: ...

    def update(self, trace: MemoryTrace) -> None: ...

    def batch_update(self, traces: list[MemoryTrace]) -> None: ...

    def remove(self, trace_id: str) -> None: ...

    def list_by_type(self, type: MemoryType) -> list[MemoryTrace]: ...

    def list_by_filter(self, filter: MemoryFilter) -> list[MemoryTrace]: ...

    def list_all(self) -> list[MemoryTrace]: ...
