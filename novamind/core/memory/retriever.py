"""Retriever Protocol — 检索策略契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import MemoryQuery, RetrievalResult


@runtime_checkable
class Retriever(Protocol):
    """检索策略协议。score = similarity × 0.7 + strength × 0.3，命中自动强化。"""

    def retrieve(self, query: MemoryQuery) -> list[RetrievalResult]: ...
