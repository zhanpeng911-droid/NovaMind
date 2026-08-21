"""LocalSecurityGuard — 严格白名单 + 路径穿越拒绝 + shell 零信任。

吸收 Poirot `sandbox/guards/local_security_guard.py` 的路径白名单 + 危险命令扫描，
融合 NovaMind 的 office 零信任底座（shell 元字符/环境变量拦截 + 命令白名单）。

INVARIANT:
- validate_path 拒 .. 段（路径穿越）
- validate_path 白名单前缀检查（PathMapping）
- validate_command shell 元字符/环境变量拦截（NovaMind 零信任，可开关）
- validate_command 命令白名单（NovaMind 零信任，可选，None 关闭走路径白名单模式）
- validate_command shlex 失败 fail-closed
- validate_command 扫描绝对路径，仅允许白名单前缀 + 系统路径
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from ..exceptions import SandboxPermissionError
from ..types import PathMapping

_SYSTEM_PATH_PREFIXES = ("/bin/", "/usr/", "/lib/")

# NovaMind 零信任：shell 元字符 / 环境变量展开拦截（Local 模式不允许重定向/管道/变量）
_SHELL_METACHARS = re.compile(r"[&|;<>`\n\r]")
_ENV_EXPANSION = re.compile(r"(%[^%]+%|\$[A-Za-z_][A-Za-z0-9_]*|\$\(|\$\{)")


class LocalSecurityGuard:
    """严格白名单 + 路径穿越拒绝 + shell 零信任。Local 模式专属。"""

    def __init__(
        self,
        path_mappings: list[PathMapping],
        *,
        command_whitelist: set[str] | None = None,
        reject_shell_metachars: bool = True,
    ) -> None:
        self._mappings = path_mappings
        self._command_whitelist = command_whitelist
        self._reject_shell_metachars = reject_shell_metachars

    def _reject_path_traversal(self, path: str) -> None:
        parts = PurePosixPath(path).parts
        if ".." in parts:
            raise SandboxPermissionError(
                f"path traversal detected: {path}", path=path, operation="validate"
            )

    def _find_mapping(self, path: str) -> PathMapping | None:
        """找 path 匹配的 PathMapping（最长前缀优先）。"""
        for mapping in sorted(
            self._mappings, key=lambda m: len(m.container_path), reverse=True
        ):
            container = mapping.container_path.rstrip("/")
            if path == container or path.startswith(container + "/"):
                return mapping
        return None

    def validate_path(self, path: str, *, write: bool = False) -> None:
        self._reject_path_traversal(path)
        mapping = self._find_mapping(path)
        if mapping is None:
            raise SandboxPermissionError(
                f"path not in whitelist: {path}", path=path, operation="validate"
            )
        if write and mapping.read_only:
            raise SandboxPermissionError(
                f"write to read-only path: {path}", path=path, operation="write"
            )

    def validate_command(self, command: str) -> None:
        # 1. NovaMind 零信任：shell 元字符 / 环境变量展开拦截
        if self._reject_shell_metachars and (
            _SHELL_METACHARS.search(command) or _ENV_EXPANSION.search(command)
        ):
            raise SandboxPermissionError(
                "shell metacharacters / env expansion blocked",
                path=command[:100],
                operation="validate_command",
            )

        # 2. shlex 解析 fail-closed
        try:
            tokens = shlex.split(command)
        except ValueError:
            raise SandboxPermissionError(
                f"command has unparseable quoting: {command[:100]}",
                path=command[:100],
                operation="validate_command",
            )
        if not tokens:
            raise SandboxPermissionError(
                "empty command", path=command[:100], operation="validate_command"
            )

        # 3. NovaMind 零信任：命令白名单（可选）
        if self._command_whitelist is not None:
            cmd = tokens[0].lower()
            if cmd not in self._command_whitelist:
                raise SandboxPermissionError(
                    f"command not in whitelist: {cmd}",
                    path=command[:100],
                    operation="validate_command",
                )

        # 4. 绝对路径白名单（defense-in-depth，Poirot）
        for token in tokens:
            if token.startswith("/"):
                if any(token.startswith(p) for p in _SYSTEM_PATH_PREFIXES):
                    continue
                if self._find_mapping(token) is None:
                    raise SandboxPermissionError(
                        f"command references path not in whitelist: {token}",
                        path=token,
                        operation="validate_command",
                    )
