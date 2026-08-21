"""跨进程文件锁 — fcntl (Unix) / msvcrt (Windows)。

吸收 Poirot `sandbox/docker/cross_process_lock.py`：仅导出 3 函数（无 context manager），
sync/async 统一用显式 open/lock/unlock/close。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def open_lock_file(lock_path: Path):
    """打开锁文件。自动创建父目录。返回 append 模式的文件对象。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return open(lock_path, "a", encoding="utf-8")


def lock_file_exclusive(lock_file) -> None:
    """获取排他锁。阻塞直到获取。Unix: flock(LOCK_EX)；Windows: msvcrt.locking。"""
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def unlock_file(lock_file) -> None:
    """释放锁。Unix: flock(LOCK_UN)；Windows: msvcrt.locking。"""
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
