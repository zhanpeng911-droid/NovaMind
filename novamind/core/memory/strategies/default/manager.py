"""DefaultMemoryManager — 四操作编排（Encode/Associate/Consolidate/Reconsolidate）。

核心原则：工具里无 LLM。merged_content / new_content 外部传入。
依赖：MemoryStore Protocol（Layer 3 实现 MarkdownFileStore）。

关键决策：
- A1：encode strength = base_strength
- B3：reconsolidate 保留原 strength + last_accessed=now
- C1：forgotten trace Retriever 过滤（L3 实现，L2 只标记 metadata.forgotten）
- D3：associate LRU 淘汰最弱关联
- E1：consolidate max=10
- F2：encode id = SHA256(content+type)[:16]（同内容同 type 去重）
"""

from __future__ import annotations

import hashlib
import time
from contextvars import ContextVar
from dataclasses import replace
from typing import Callable

from ...config import get_memory_config
from ...exceptions import MemoryNotFoundError
from ...memory_store import MemoryStore
from ...schema import Association, MemoryTrace, MemoryType, OperationLog
from ._constants import ASSOCIATE_DEFAULTS, CONSOLIDATE_PARAMS, DECAY_PARAMS
from .decay import EbbinghausDecayPolicy
from .forget import CompositeForgetPolicy

_turn_id_var: ContextVar[str | None] = ContextVar("memory_turn_id", default=None)


def set_turn_id(turn_id: str | None) -> None:
    """L4 MemoryMiddleware 调用，设置当前 turn_id。"""
    _turn_id_var.set(turn_id)


class DefaultMemoryManager:
    """默认记忆管理器（四操作）。无 LLM，frozen 语义。"""

    def __init__(
        self,
        store: MemoryStore,
        *,
        decay_policy: EbbinghausDecayPolicy | None = None,
        forget_policy: CompositeForgetPolicy | None = None,
        journal: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._store = store
        self._decay_policy = decay_policy or EbbinghausDecayPolicy()
        self._forget_policy = forget_policy or CompositeForgetPolicy(self._decay_policy)
        self._journal = journal

    @staticmethod
    def _compute_trace_id(content: str, type: MemoryType) -> str:
        type_key = type.value if isinstance(type, MemoryType) else str(type)
        raw = f"{content}\x00{type_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _add_association_with_lru(trace: MemoryTrace, new_assoc: Association, max_assocs: int) -> MemoryTrace:
        current = trace.associations
        if len(current) >= max_assocs:
            sorted_assocs = sorted(current, key=lambda a: a.strength, reverse=True)
            kept = tuple(sorted_assocs[: max_assocs - 1]) + (new_assoc,)
            return replace(trace, associations=kept)
        return replace(trace, associations=current + (new_assoc,))

    def _get_actor(self) -> str | None:
        return _turn_id_var.get()

    def _emit_journal(self, event: str, payload: dict) -> None:
        if self._journal is not None:
            self._journal(event, payload)

    def _get_decay_params(self, type: MemoryType) -> dict:
        config = get_memory_config()
        type_key = type.value if isinstance(type, MemoryType) else str(type)
        if hasattr(config, "decay") and type_key in config.decay:
            return config.decay[type_key]
        return DECAY_PARAMS[type_key]

    def encode(
        self,
        content: str,
        type: MemoryType,
        *,
        importance: float = 0.5,
        source: str | None = None,
        metadata: dict | None = None,
    ) -> MemoryTrace:
        trace_id = self._compute_trace_id(content, type)

        existing = self._store.get(trace_id)
        if existing is not None:
            self._emit_journal("memory.encode.duplicate", {
                "trace_id": trace_id, "type": type.value, "content_preview": content[:100],
            })
            return existing

        now = time.time()
        params = self._get_decay_params(type)
        actor = self._get_actor()
        trace = MemoryTrace(
            id=trace_id,
            content=content,
            type=type,
            strength=params["base_strength"],
            base_strength=params["base_strength"],
            decay_rate=params["decay_rate"],
            access_count=0,
            last_accessed=now,
            importance=importance,
            source=source,
            created_at=now,
            metadata=metadata or {},
            operation_log=(OperationLog(
                timestamp=now, operation="encode", actor=actor,
                diff={"content": (None, content[:200]), "type": (None, type.value)},
            ),),
        )
        self._store.add(trace)
        self._emit_journal("memory.encode", {
            "trace_id": trace_id, "type": type.value,
            "content_preview": content[:100], "timestamp": now, "actor": actor,
        })
        return trace

    def associate(
        self,
        trace_id_a: str,
        trace_id_b: str,
        *,
        strength: float | None = None,
        type: str | None = None,
    ) -> None:
        defaults = ASSOCIATE_DEFAULTS
        assoc_strength = strength if strength is not None else defaults["default_strength"]
        assoc_type = type if type is not None else defaults["default_type"]
        max_assocs = defaults["max_associations_per_trace"]

        trace_a = self._store.get(trace_id_a)
        if trace_a is None:
            raise MemoryNotFoundError(trace_id_a)
        trace_b = self._store.get(trace_id_b)
        if trace_b is None:
            raise MemoryNotFoundError(trace_id_b)

        now = time.time()
        actor = self._get_actor()
        new_assoc_a = Association(target_id=trace_id_b, strength=assoc_strength, type=assoc_type)
        new_assoc_b = Association(target_id=trace_id_a, strength=assoc_strength, type=assoc_type)

        updated_a = self._add_association_with_lru(trace_a, new_assoc_a, max_assocs)
        updated_b = self._add_association_with_lru(trace_b, new_assoc_b, max_assocs)

        updated_a = updated_a.with_operation(OperationLog(
            timestamp=now, operation="associate", actor=actor,
            diff={"target": trace_id_b, "strength": assoc_strength, "type": assoc_type},
        ))
        updated_b = updated_b.with_operation(OperationLog(
            timestamp=now, operation="associate", actor=actor,
            diff={"target": trace_id_a, "strength": assoc_strength, "type": assoc_type},
        ))

        self._store.update(updated_a)
        self._store.update(updated_b)
        self._emit_journal("memory.associate", {
            "trace_id_a": trace_id_a, "trace_id_b": trace_id_b,
            "strength": assoc_strength, "type": assoc_type,
            "timestamp": now, "actor": actor,
        })

    def consolidate(self, trace_ids: list[str], merged_content: str) -> MemoryTrace:
        params = CONSOLIDATE_PARAMS

        if len(trace_ids) < params["min_traces_to_consolidate"]:
            raise ValueError(f"consolidate requires at least {params['min_traces_to_consolidate']} traces")
        if len(trace_ids) > params["max_traces_to_consolidate"]:
            raise ValueError(f"consolidate allows at most {params['max_traces_to_consolidate']} traces")

        old_traces: list[MemoryTrace] = []
        for tid in trace_ids:
            t = self._store.get(tid)
            if t is None:
                raise MemoryNotFoundError(tid)
            old_traces.append(t)

        max_importance = max(t.importance for t in old_traces)
        new_importance = min(1.0, max_importance + params["default_importance_boost"])

        new_trace_id = self._compute_trace_id(merged_content, MemoryType.SEMANTIC)
        existing = self._store.get(new_trace_id)
        if existing is not None:
            self._emit_journal("memory.consolidate.duplicate", {
                "trace_id": new_trace_id, "old_trace_ids": trace_ids,
                "merged_content_preview": merged_content[:100],
            })
            return existing

        all_target_ids: set[str] = set()
        for t in old_traces:
            for assoc in t.associations:
                if assoc.target_id not in trace_ids:
                    all_target_ids.add(assoc.target_id)
        new_associations = tuple(
            Association(target_id=tid, strength=0.5, type="related") for tid in all_target_ids
        )

        now = time.time()
        actor = self._get_actor()
        semantic_params = self._get_decay_params(MemoryType.SEMANTIC)
        new_trace = MemoryTrace(
            id=new_trace_id,
            content=merged_content,
            type=MemoryType.SEMANTIC,
            strength=semantic_params["base_strength"],
            base_strength=semantic_params["base_strength"],
            decay_rate=semantic_params["decay_rate"],
            access_count=0,
            last_accessed=now,
            importance=new_importance,
            associations=new_associations,
            source=f"consolidate:{','.join(trace_ids)}",
            created_at=now,
            metadata={"consolidated_from": trace_ids},
            operation_log=(OperationLog(
                timestamp=now, operation="consolidate", actor=actor,
                diff={"consolidated_from": trace_ids, "merged_content": (None, merged_content[:200])},
            ),),
        )
        self._store.add(new_trace)

        forgotten_traces: list[MemoryTrace] = []
        for old_trace in old_traces:
            forgotten = replace(
                old_trace,
                metadata={**old_trace.metadata, "forgotten": True, "consolidated_into": new_trace.id},
            )
            forgotten = forgotten.with_operation(OperationLog(
                timestamp=now, operation="forget", actor=actor,
                diff={"reason": "consolidated", "consolidated_into": new_trace.id},
            ))
            forgotten_traces.append(forgotten)
        self._store.batch_update(forgotten_traces)

        self._emit_journal("memory.consolidate", {
            "new_trace_id": new_trace_id, "old_trace_ids": trace_ids,
            "merged_content_preview": merged_content[:100], "timestamp": now, "actor": actor,
        })
        return new_trace

    def reconsolidate(self, trace_id: str, new_content: str) -> MemoryTrace:
        old_trace = self._store.get(trace_id)
        if old_trace is None:
            raise MemoryNotFoundError(trace_id)

        now = time.time()
        actor = self._get_actor()
        updated = replace(
            old_trace,
            content=new_content,
            last_accessed=now,
            metadata={**old_trace.metadata, "reconsolidated_at": now},
        )
        updated = updated.with_operation(OperationLog(
            timestamp=now, operation="reconsolidate", actor=actor,
            diff={"content": (old_trace.content[:200], new_content[:200]), "strength": (old_trace.strength, old_trace.strength)},
        ))
        self._store.update(updated)
        self._emit_journal("memory.reconsolidate", {
            "trace_id": trace_id,
            "old_content_preview": old_trace.content[:100],
            "new_content_preview": new_content[:100],
            "timestamp": now, "actor": actor,
        })
        return updated
