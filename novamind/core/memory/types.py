"""记忆辅助类型：MemoryQuery / MemoryFilter / RetrievalResult。

INVARIANT: RetrievalResult.score 复合分数 score = similarity * 0.7 + strength * 0.3。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema import MemoryTrace, MemoryType


@dataclass(frozen=True)
class MemoryQuery:
    """检索查询。"""

    text: str
    top_k: int = 5
    type_filter: MemoryType | None = None
    min_strength: float = 0.0
    metadata_filter: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryFilter:
    """记忆过滤（遗忘策略用）。"""

    type_filter: MemoryType | None = None
    min_strength: float = 0.0
    max_age_hours: float | None = None
    metadata_filter: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalResult:
    """检索结果。"""

    trace: MemoryTrace
    similarity: float
    strength: float
    score: float

    @classmethod
    def compute_score(
        cls, trace: MemoryTrace, similarity: float, strength: float
    ) -> "RetrievalResult":
        score = similarity * 0.7 + strength * 0.3
        return cls(trace=trace, similarity=similarity, strength=strength, score=score)
