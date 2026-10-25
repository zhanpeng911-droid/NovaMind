"""治理服务运行时单例（B1/B2）。

默认栈与 WebUI Memory Center 必须共用同一个治理服务/同一个 SQLite 文件，
否则会出现「API 改了状态、召回层看不到」的分裂。本模块提供进程级懒加载
单例；测试可 reset。

provider 解析顺序：
1. 显式传入；
2. bootstrap 的记忆 provider（config.use 非空时的正常路径）；
3. 强制构建默认 provider（config.use 为空但默认栈仍需要记忆能力的兼容路径）。
"""

from __future__ import annotations

import threading
from typing import Any

from .governance import MemoryGovernanceService, MemoryGovernanceStore

_lock = threading.Lock()
_service: MemoryGovernanceService | None = None


def get_governance_service(provider: Any = None) -> MemoryGovernanceService:
    global _service
    if _service is not None:
        return _service
    with _lock:
        if _service is not None:
            return _service
        from ..personal_upgrade import ensure_snapshot
        ensure_snapshot()
        if provider is None:
            from .bootstrap import get_or_build_memory_provider

            provider = get_or_build_memory_provider()
        from ..config import MEMORY_GOVERNANCE_DB_PATH

        store = MemoryGovernanceStore(MEMORY_GOVERNANCE_DB_PATH)
        service = MemoryGovernanceService(
            store, trace_store=provider.store(), retriever=provider.retriever(),
        )
        from ..personal import settings, update_settings
        if not settings()["memory"].get("migration_complete"):
            service.migrate_legacy(provider.store().list_all())
            _migrate_profile(service)
            update_settings("memory", {"migration_complete": True})
        _service = service
        return service


def _migrate_profile(service):
    from ..personal import request_context
    from ..config import PROFILE_PATH
    from pathlib import Path
    token = request_context.set({})  # Migration is not a write inferred from the current private turn.
    try:
        legacy = Path(PROFILE_PATH)
        if legacy.is_file():
            for line in legacy.read_text(encoding="utf-8").splitlines():
                value = line.strip().lstrip("- *")
                if value and not value.startswith("#"):
                    try:
                        service.save_explicit(value, source_thread="legacy_profile")
                    except ValueError:
                        pass  # Original/backup remains; invalid or sensitive bodies are not promoted.
    finally:
        request_context.reset(token)


def reset_governance_service() -> None:
    global _service
    with _lock:
        _service = None


def shutdown_governance_service() -> None:
    global _service
    with _lock:
        if _service is not None:
            _service.store.close()
            _service = None
