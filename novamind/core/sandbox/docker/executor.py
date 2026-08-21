"""Docker 执行器 — 路径翻译（WSL2 / 本地 daemon）。

吸收 Poirot `sandbox/docker/executor.py` 的 WslDockerExecutor：
Windows + WSL2 Docker daemon 场景下，把 Windows 路径（D:\\foo\\bar）翻译为
WSL2 挂载路径（/mnt/d/foo/bar），否则 bind mount 会失败。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def translate_to_wsl(path: str) -> str:
    """Windows 路径 → WSL2 挂载路径。

    D:\\foo\\bar → /mnt/d/foo/bar
    非 Windows 盘符路径直传。
    """
    normalized = path.replace("\\", "/")
    m = _WIN_DRIVE_RE.match(normalized)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2)
        return f"/mnt/{drive}/{rest}"
    return normalized


class WslDockerExecutor:
    """WSL2 Docker executor：翻译宿主机路径为 WSL2 挂载路径供 docker 命令使用。"""

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled or os.environ.get("NOVAMIND_SANDBOX_EXECUTOR", "").lower() == "wsl"

    @property
    def enabled(self) -> bool:
        return self._enabled

    def translate_path(self, path: str) -> str:
        """按需翻译路径。未启用时直传。"""
        if not self._enabled:
            return path
        return translate_to_wsl(path)

    def translate(self, paths: list[str]) -> list[str]:
        return [self.translate_path(p) for p in paths]
