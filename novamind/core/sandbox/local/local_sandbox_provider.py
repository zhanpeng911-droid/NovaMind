"""LocalSandboxProvider — LRU 缓存 + 确定性 ID + office 工位映射。

吸收 Poirot `sandbox/local/local_sandbox_provider.py`：
- acquire(thread_id) → per-thread sandbox，确定性 ID = sha256(user:thread)[:8]
- LRU 缓存默认 256 条，超出按 LRU 驱逐
- release no-op（保留缓存复用）
- 构造 Sandbox 时组合 LocalRuntime + LocalPathTranslator + AuditGuard(LocalSecurityGuard)

融合 NovaMind：默认映射 office 工位（虚拟 /mnt/novamind/user_data → OFFICE_DIR）。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

from ..contracts import SandboxProvider
from ..guards.audit_guard import AuditGuard
from ..guards.local_security_guard import LocalSecurityGuard
from ..runtimes.local_runtime import LocalRuntime
from ..sandbox import Sandbox
from ..translators.local_path_translator import LocalPathTranslator
from ..types import PathMapping

_DEFAULT_LRU_SIZE = 256

# NovaMind 零信任 shell 白名单（office 工位专用，与旧 execute_office_shell 一致）
NOVAMIND_SHELL_WHITELIST = {"pwd", "echo", "ls", "dir", "cat", "type", "mkdir"}


def default_office_path_mappings() -> list[PathMapping]:
    """默认 office 工位映射：虚拟 /mnt/novamind/user_data → OFFICE_DIR。"""
    from ...config import OFFICE_DIR

    return [PathMapping(container_path="/mnt/novamind/user_data", local_path=OFFICE_DIR)]


def _deterministic_sandbox_id(user_id: str | None, thread_id: str | None) -> str:
    """确定性 sandbox_id = sha256(user:thread)[:8]。跨进程可推导。"""
    raw = f"{user_id or 'default'}:{thread_id or 'default'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


class LocalSandboxProvider(SandboxProvider):
    """LocalSandboxProvider — LRU 缓存 + 确定性 ID。"""

    uses_thread_data_mounts = True
    needs_upload_permission_adjustment = False

    def __init__(
        self,
        path_mappings: list[PathMapping] | None = None,
        lru_size: int = _DEFAULT_LRU_SIZE,
        *,
        command_whitelist: set[str] | None = None,
        allow_host_bash: bool = True,
    ) -> None:
        self._path_mappings = path_mappings if path_mappings is not None else default_office_path_mappings()
        self._lru_size = lru_size
        self._command_whitelist = command_whitelist
        self._allow_host_bash = allow_host_bash
        self._sandboxes: OrderedDict[str, Sandbox] = OrderedDict()
        self._lock = threading.Lock()

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        if thread_id is None:
            raise ValueError("thread_id is required")
        sandbox_id = _deterministic_sandbox_id(user_id, thread_id)

        evicted: Sandbox | None = None
        with self._lock:
            if sandbox_id in self._sandboxes:
                self._sandboxes.move_to_end(sandbox_id)
                return sandbox_id

            runtime = LocalRuntime(allow_host_bash=self._allow_host_bash)
            translator = LocalPathTranslator(self._path_mappings)
            guard = AuditGuard(
                LocalSecurityGuard(
                    self._path_mappings,
                    command_whitelist=self._command_whitelist,
                )
            )
            sandbox = Sandbox(sandbox_id, runtime, translator, guard)
            self._sandboxes[sandbox_id] = sandbox

            if len(self._sandboxes) > self._lru_size:
                _evicted_id, evicted = self._sandboxes.popitem(last=False)

        if evicted is not None:
            evicted.close()
        return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        pass

    def reset(self) -> None:
        with self._lock:
            self._sandboxes.clear()

    def shutdown(self) -> None:
        with self._lock:
            sandboxes = list(self._sandboxes.values())
            self._sandboxes.clear()
        for sandbox in sandboxes:
            sandbox.close()
