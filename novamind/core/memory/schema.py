"""MemoryTrace — 记忆原子单元（frozen dataclass）。

吸收 Poirot `memory/schema.py`：
- MemoryTrace 不可变：strength 等可变字段通过 with_strength()/with_operation() 创建新实例
- operation_log traceability：上限 20 条 FIFO；retrieve 不记（高频）
- actor 字段预留 turn_id（L4 Middleware 注入）
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    """记忆类型（认知科学映射）。"""

    EPISODIC = "episodic"        # 事件记忆：衰减快，需反复检索强化
    SEMANTIC = "semantic"        # 语义记忆：衰减慢，提炼后的稳定知识
    PROCEDURAL = "procedural"    # 过程记忆：几乎不衰减，习得的技能/画像


@dataclass(frozen=True)
class Association:
    """记忆关联（扩散激活用）。"""

    target_id: str
    strength: float = 0.5
    type: str = "related"


@dataclass(frozen=True)
class OperationLog:
    """操作日志条目（traceability）。retrieve 不记。"""

    timestamp: float
    operation: str                        # encode/associate/consolidate/reconsolidate/forget
    actor: str | None = None              # thread_id / turn_id（L4 注入）
    diff: dict[str, Any] | None = None


@dataclass(frozen=True)
class MemoryTrace:
    """记忆痕迹。可变字段通过 with_strength()/with_operation() 创建新实例替换。"""

    id: str
    content: str
    type: MemoryType
    strength: float = 0.0
    base_strength: float = 0.7
    decay_rate: float = 0.1
    access_count: int = 0
    last_accessed: float = 0.0
    importance: float = 0.5
    associations: tuple[Association, ...] = field(default_factory=tuple)
    embedding: tuple[float, ...] | None = None
    source: str | None = None
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    operation_log: tuple[OperationLog, ...] = field(default_factory=tuple)

    def with_strength(self, new_strength: float, accessed_at: float) -> "MemoryTrace":
        """检索强化：strength + access_count + last_accessed。"""
        return replace(
            self,
            strength=new_strength,
            access_count=self.access_count + 1,
            last_accessed=accessed_at,
        )

    def with_operation(self, log: OperationLog, *, max_log: int = 20) -> "MemoryTrace":
        """append 一条操作日志（上限 20 条 FIFO）。"""
        new_log = self.operation_log + (log,)
        if len(new_log) > max_log:
            new_log = new_log[-max_log:]
        return replace(self, operation_log=new_log)
