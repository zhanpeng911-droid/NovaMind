"""Memory bootstrap lifecycle — get/reset/shutdown/set + 懒加载 + worker lifecycle。"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .config import get_memory_config

logger = logging.getLogger(__name__)

_provider_lock = threading.Lock()
_memory_provider: Any = None


def get_memory_provider() -> Any:
    """懒加载 + 双检锁。config.use 为空时返 None（记忆禁用）。"""
    global _memory_provider
    if _memory_provider is not None:
        return _memory_provider
    with _provider_lock:
        if _memory_provider is not None:
            return _memory_provider
        config = get_memory_config()
        if not config.use:
            return None
        provider = _load_memory_provider(config)
        _memory_provider = provider
        return provider


def reset_memory_provider() -> None:
    global _memory_provider
    with _provider_lock:
        _memory_provider = None


def shutdown_memory_provider() -> None:
    global _memory_provider
    with _provider_lock:
        if _memory_provider is not None:
            if hasattr(_memory_provider, "shutdown"):
                _memory_provider.shutdown()
            _memory_provider = None


def set_memory_provider(provider: Any) -> None:
    global _memory_provider
    with _provider_lock:
        _memory_provider = provider


# ── L5 worker lifecycle ──────────────────────────────────────────────

_worker_lock = threading.Lock()
_memory_worker: Any = None


def start_memory_worker(manager: Any, llm: Any) -> Any:
    global _memory_worker
    if _memory_worker is not None:
        return _memory_worker
    from .worker import MemoryWorker

    with _worker_lock:
        if _memory_worker is not None:
            return _memory_worker
        worker = MemoryWorker(manager=manager, llm=llm)
        worker.start()
        _memory_worker = worker
        return worker


def shutdown_memory_worker(timeout: float = 5.0) -> None:
    global _memory_worker
    with _worker_lock:
        if _memory_worker is None:
            return
        _memory_worker.shutdown(timeout=timeout)
        _memory_worker = None


def get_memory_worker() -> Any:
    with _worker_lock:
        return _memory_worker


def _load_memory_provider(config: Any) -> Any:
    from .strategies.default.strategy import _wrap_store, build_default_provider

    journal = _make_journal_callback()
    provider = build_default_provider(journal=journal)
    _wrap_store(provider.store(), provider.retriever())
    return provider


def _make_journal_callback() -> Any:
    return None  # NovaMind 暂无 RunJournal 注入，后续接入审计时补充


