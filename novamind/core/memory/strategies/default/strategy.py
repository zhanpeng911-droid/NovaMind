"""默认 MemoryProvider 主入口（组合 store + retriever + manager）。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ....config import WORKSPACE_DIR
from ...config import get_memory_config
from ...memory_store import MemoryStore
from ...retriever import Retriever
from .decay import EbbinghausDecayPolicy
from .forget import CompositeForgetPolicy
from .manager import DefaultMemoryManager
from .retriever import HybridRetriever
from .store import MarkdownFileStore


def _resolve_storage_root(storage_path: str) -> Path:
    """相对路径收敛到统一数据根（WORKSPACE_DIR），不再跟随 CWD；绝对路径保留。"""
    p = Path(storage_path)
    if p.is_absolute():
        return p
    return Path(WORKSPACE_DIR) / p


def _migrate_legacy_storage(target: Path) -> None:
    """旧版 storage_path 默认 `.novamind/memory`（相对 CWD）落盘：
    若新位置尚无数据且旧位置存在 traces，一次性迁移，避免跨目录启动后记忆"消失"。
    失败静默，不阻塞启动。"""
    legacy = Path.cwd() / ".novamind" / "memory"
    if not legacy.exists():
        return
    if (target / "traces.md").exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
    except Exception:
        pass


@dataclass(frozen=True)
class DefaultMemoryProvider:
    """默认 MemoryProvider 实现（组合 store + retriever + manager）。"""

    _store: MemoryStore
    _retriever: Retriever
    _manager: DefaultMemoryManager

    def store(self) -> MemoryStore:
        return self._store

    def retriever(self) -> Retriever:
        return self._retriever

    def manager(self) -> DefaultMemoryManager:
        return self._manager

    def shutdown(self) -> None:
        if hasattr(self._store, "shutdown"):
            self._store.shutdown()
        if hasattr(self._retriever, "shutdown"):
            self._retriever.shutdown()


def _wrap_store(store, retriever) -> None:
    """把 store 的增/改/批量改/删包装为 retriever 增量索引更新（按 retriever 幂等）。

    缺陷#1 修复：build_default_provider 默认路径此前未接线，encode 后新记忆
    不进入 HybridRetriever 索引，retrieve 恒 0 命中。bootstrap 同用本函数，单一来源。
    """
    wired = getattr(store, "_wired_retrievers", None)
    if wired is None:
        wired = store._wired_retrievers = set()
    rid = id(retriever)
    if rid in wired:
        return  # 该 retriever 已接线，避免重复包装
    wired.add(rid)

    originals = {m: getattr(store, m) for m in ("add", "update", "batch_update", "remove")}

    def wrapped_add(trace):
        originals["add"](trace)
        retriever.on_trace_added(trace)

    def wrapped_update(trace):
        originals["update"](trace)
        retriever.on_trace_updated(trace)

    def wrapped_batch_update(traces):
        originals["batch_update"](traces)
        for t in traces:
            retriever.on_trace_updated(t)

    def wrapped_remove(trace_id):
        originals["remove"](trace_id)
        retriever.on_trace_removed(trace_id)

    store.add = wrapped_add
    store.update = wrapped_update
    store.batch_update = wrapped_batch_update
    store.remove = wrapped_remove


def build_default_provider(
    *,
    store: MemoryStore | None = None,
    retriever: Retriever | None = None,
    decay_policy: EbbinghausDecayPolicy | None = None,
    forget_policy: CompositeForgetPolicy | None = None,
    journal: Callable[[str, dict], None] | None = None,
) -> DefaultMemoryProvider:
    """组装默认 MemoryProvider。store/retriever 未注入时从 config 实例化。"""
    config = get_memory_config()
    decay = decay_policy or EbbinghausDecayPolicy()
    forget = forget_policy or CompositeForgetPolicy(decay)

    if store is None:
        root = _resolve_storage_root(config.storage_path)
        _migrate_legacy_storage(root)
        store = MarkdownFileStore(root)
    if retriever is None:
        retriever = HybridRetriever(store, decay)

    # 缺陷#1 修复：默认路径也接入增量索引（幂等，调用方已接线的 store 不受影响）
    _wrap_store(store, retriever)

    manager = DefaultMemoryManager(
        store=store,
        decay_policy=decay,
        forget_policy=forget,
        journal=journal,
    )
    return DefaultMemoryProvider(_store=store, _retriever=retriever, _manager=manager)
