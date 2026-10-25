"""跨平台单实例锁（A0）。

Windows 用命名互斥量（CreateMutexW + ERROR_ALREADY_EXISTS），
POSIX 用临时目录锁文件 + flock。第二个实例 acquire 返回 False，
由调用方决定「唤醒既有实例」还是退出。
"""

from __future__ import annotations

import os
import sys
import tempfile

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateMutexW = _kernel32.CreateMutexW
    _CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _CreateMutexW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """进程级单实例锁；acquired 为 True 表示当前进程持有锁。"""

    def __init__(self, name: str = "Local\\NovaMind.Desktop.SingleInstance") -> None:
        self._name = name
        self._handle = None
        self._lock_file = None

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if sys.platform == "win32":
            handle = _CreateMutexW(None, False, self._name)
            if not handle:
                return False
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                _CloseHandle(handle)
                return False
            self._handle = handle
            return True
        path = os.path.join(
            tempfile.gettempdir(),
            self._name.replace("\\", "_").replace(":", "_") + ".lock",
        )
        lock_file = open(path, "a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False
        self._lock_file = lock_file
        return True

    @property
    def acquired(self) -> bool:
        return self._handle is not None or self._lock_file is not None

    def release(self) -> None:
        if sys.platform == "win32":
            if self._handle is not None:
                _CloseHandle(self._handle)
                self._handle = None
            return
        if self._lock_file is not None:
            try:
                import fcntl

                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError("another NovaMind instance is already running")
        return self

    def __exit__(self, *exc) -> None:
        self.release()
