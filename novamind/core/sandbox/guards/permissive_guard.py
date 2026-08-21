"""PermissiveGuard — 宽松守卫（Docker 容器隔离兜底）。"""

from __future__ import annotations


class PermissiveGuard:
    """宽松守卫：路径与命令都不拦截（Docker/E2B 容器内已隔离）。"""

    def validate_path(self, path: str, *, write: bool = False) -> None:
        return None

    def validate_command(self, command: str) -> None:
        return None
