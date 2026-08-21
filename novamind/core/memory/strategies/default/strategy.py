"""默认 MemoryProvider 主入口（组合 store + retriever + manager）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ...config import get_memory_config
from ...memory_store import MemoryStore
from ...retriever import Retriever
from .decay import EbbinghausDecayPolicy
from .forget import CompositeForgetPolicy
from .manager import DefaultMemoryManager
from .retriever import HybridRetriever
from .store import MarkdownFileStore


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
        store = MarkdownFileStore(config.storage_path)
    if retriever is None:
        retriever = HybridRetriever(store, decay)

    manager = DefaultMemoryManager(
        store=store,
        decay_policy=decay,
        forget_policy=forget,
        journal=journal,
    )
    return DefaultMemoryProvider(_store=store, _retriever=retriever, _manager=manager)
