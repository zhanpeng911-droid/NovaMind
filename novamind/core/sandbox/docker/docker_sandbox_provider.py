"""DockerSandboxProvider — warm pool + idle auto-destroy + 跨进程锁 + WSL2。

吸收 Poirot `sandbox/docker/docker_sandbox_provider.py` 的生命周期管理：
- 确定性 sandbox_id（与 Local 一致）
- warm pool：release 后容器保持运行不销毁，同一 sandbox_id 再次 acquire 时直接复用（跳过
  docker run 冷启动）；空闲容器数量受 warm_pool_size 上限约束，超限淘汰最旧
- idle auto-destroy：后台线程定期销毁空闲超时（默认 600s）的 warm 与 active 容器
- 跨进程锁保证多实例并发安全
- WSL2 executor 翻译 Windows 路径

说明：真实运行需要 Docker 环境。本类保证接口与 LocalSandboxProvider 一致，可切换。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path

from ..contracts import SandboxProvider
from ..guards.audit_guard import AuditGuard
from ..guards.docker_path_guard import DockerPathGuard
from ..runtimes.docker_runtime import DockerRuntime
from ..sandbox import Sandbox
from ..translators.docker_path_translator import DockerPathTranslator, VIRTUAL_PREFIX
from ..types import SandboxInfo
from .cross_process_lock import lock_file_exclusive, open_lock_file, unlock_file
from .executor import WslDockerExecutor

_DEFAULT_IDLE_TIMEOUT = 600
_DEFAULT_WARM_POOL_SIZE = 2
_IDLE_CHECK_INTERVAL = 60


def _deterministic_sandbox_id(user_id: str | None, thread_id: str | None) -> str:
    raw = f"{user_id or 'default'}:{thread_id or 'default'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


class DockerSandboxProvider(SandboxProvider):
    """DockerSandboxProvider — 容器生命周期 + warm pool + idle destroy。"""

    uses_thread_data_mounts = True
    needs_upload_permission_adjustment = True

    def __init__(
        self,
        sandbox_root: str,
        *,
        image: str = "novamind-sandbox:latest",
        warm_pool_size: int = _DEFAULT_WARM_POOL_SIZE,
        idle_timeout: int = _DEFAULT_IDLE_TIMEOUT,
        docker_cmd: str = "docker",
        executor: WslDockerExecutor | None = None,
    ) -> None:
        self._sandbox_root = sandbox_root
        self._image = image
        self._warm_pool_size = max(0, warm_pool_size)
        self._idle_timeout = idle_timeout
        self._docker_cmd = docker_cmd
        self._executor = executor or WslDockerExecutor()

        self._sandboxes: dict[str, Sandbox] = {}          # active
        self._warm_pool: dict[str, tuple[Sandbox, float]] = {}  # 空闲（sandbox_id -> (sandbox, release_ts)）
        self._containers: dict[str, str] = {}             # sandbox_id -> container 名
        self._last_used: dict[str, float] = {}            # active 最后使用时间
        self._lock = threading.Lock()

        self._idle_stop = threading.Event()
        self._idle_thread: threading.Thread | None = None

        os.makedirs(self._sandbox_root, exist_ok=True)
        if idle_timeout > 0:
            self._start_idle_checker()

    # ── 生命周期 ───────────────────────────────────────────────

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        if thread_id is None:
            raise ValueError("thread_id is required")
        sandbox_id = _deterministic_sandbox_id(user_id, thread_id)

        with self._lock:
            # 1) active 复用
            if sandbox_id in self._sandboxes:
                self._last_used[sandbox_id] = time.time()
                return sandbox_id
            # 2) warm pool 复用（跳过 docker run 冷启动）
            if sandbox_id in self._warm_pool:
                sandbox, _ = self._warm_pool.pop(sandbox_id)
                self._sandboxes[sandbox_id] = sandbox
                self._last_used[sandbox_id] = time.time()
                return sandbox_id

        # 3) 冷启动创建
        container = self._create_container(sandbox_id)
        runtime = DockerRuntime(container, self._docker_cmd)
        translator = DockerPathTranslator(self._sandbox_root, sandbox_id)
        guard = AuditGuard(DockerPathGuard())
        sandbox = Sandbox(sandbox_id, runtime, translator, guard)

        with self._lock:
            self._sandboxes[sandbox_id] = sandbox
            self._containers[sandbox_id] = container
            self._last_used[sandbox_id] = time.time()

        return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is not None:
                self._last_used[sandbox_id] = time.time()
            return sandbox

    def release(self, sandbox_id: str) -> None:
        """释放：移入 warm pool（容器保持运行，不销毁），空闲超时再销毁。"""
        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
            self._last_used.pop(sandbox_id, None)
            if sandbox is None:
                return
            # 超限淘汰最旧 warm
            if len(self._warm_pool) >= self._warm_pool_size:
                self._evict_oldest_warm_locked()
            self._warm_pool[sandbox_id] = (sandbox, time.time())

    def get_sandbox_info(self, sandbox_id: str) -> SandboxInfo | None:
        with self._lock:
            container = self._containers.get(sandbox_id)
            if container is None:
                return None
        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=f"{VIRTUAL_PREFIX}",
            container_name=container,
        )

    def reset(self) -> None:
        with self._lock:
            sandboxes = list(self._sandboxes.values()) + [s for s, _ in self._warm_pool.values()]
            self._sandboxes.clear()
            self._warm_pool.clear()
            self._containers.clear()
            self._last_used.clear()
        for sandbox in sandboxes:
            sandbox.close()

    def shutdown(self) -> None:
        self._idle_stop.set()
        if self._idle_thread and self._idle_thread.is_alive():
            self._idle_thread.join(timeout=5)

        with self._lock:
            sandboxes = list(self._sandboxes.values()) + [s for s, _ in self._warm_pool.values()]
            containers = list(self._containers.values())
            self._sandboxes.clear()
            self._warm_pool.clear()
            self._containers.clear()
            self._last_used.clear()
        for container in containers:
            self._destroy_container(container)
        for sandbox in sandboxes:
            sandbox.close()

    # ── 内部：容器生命周期 ─────────────────────────────────────

    def _host_mount_for(self, sandbox_id: str) -> str:
        """宿主挂载路径（WSL2 翻译后）。"""
        host_path = os.path.join(self._sandbox_root, sandbox_id)
        return self._executor.translate_path(host_path)

    def _create_container(self, sandbox_id: str) -> str:
        """docker run -d 创建容器，挂载 /mnt/novamind/user_data。"""
        host_mount = self._host_mount_for(sandbox_id)
        os.makedirs(os.path.join(self._sandbox_root, sandbox_id), exist_ok=True)

        container = f"novamind-{sandbox_id}"

        # 跨进程锁：避免并发创建同名容器
        lock_path = os.path.join(self._sandbox_root, ".lock")
        lock_file = open_lock_file(Path(lock_path))
        try:
            lock_file_exclusive(lock_file)
            subprocess.run(
                [self._docker_cmd, "run", "-d", "--name", container,
                 "-v", f"{host_mount}:{VIRTUAL_PREFIX}", self._image, "sleep", "infinity"],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"docker not found: {self._docker_cmd}") from exc
        finally:
            unlock_file(lock_file)
            lock_file.close()
        return container

    def _destroy_container(self, container: str) -> None:
        try:
            subprocess.run(
                [self._docker_cmd, "rm", "-f", container],
                check=True, capture_output=True, text=True,
            )
        except Exception:
            pass  # 销毁失败不阻塞 shutdown

    def _evict_oldest_warm_locked(self) -> None:
        """淘汰最旧 warm（调用方须持锁）。"""
        if not self._warm_pool:
            return
        oldest = min(self._warm_pool, key=lambda sid: self._warm_pool[sid][1])
        sandbox, _ = self._warm_pool.pop(oldest)
        container = self._containers.pop(oldest, None)
        try:
            sandbox.close()
        except Exception:
            pass
        if container:
            self._destroy_container(container)

    # ── idle auto-destroy ──────────────────────────────────────

    def _start_idle_checker(self) -> None:
        self._idle_thread = threading.Thread(
            target=self._idle_loop, name="sandbox-idle-checker", daemon=True,
        )
        self._idle_thread.start()

    def _idle_loop(self) -> None:
        while not self._idle_stop.wait(timeout=_IDLE_CHECK_INTERVAL):
            try:
                self._cleanup_idle()
            except Exception:
                pass

    def _cleanup_idle(self) -> None:
        now = time.time()
        warm_to_destroy: list[str] = []
        active_to_destroy: list[str] = []
        with self._lock:
            for sid, (_, ts) in list(self._warm_pool.items()):
                if now - ts > self._idle_timeout:
                    warm_to_destroy.append(sid)
                    self._warm_pool.pop(sid, None)
            for sid, ts in list(self._last_used.items()):
                if now - ts > self._idle_timeout:
                    active_to_destroy.append(sid)

        for sid in warm_to_destroy:
            container = self._containers.pop(sid, None)
            if container:
                self._destroy_container(container)
        for sid in active_to_destroy:
            self._destroy_active(sid)

    def _destroy_active(self, sandbox_id: str) -> None:
        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
            self._last_used.pop(sandbox_id, None)
            container = self._containers.pop(sandbox_id, None)
        if sandbox:
            try:
                sandbox.close()
            except Exception:
                pass
        if container:
            self._destroy_container(container)
