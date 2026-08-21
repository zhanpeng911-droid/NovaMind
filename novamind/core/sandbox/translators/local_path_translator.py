"""LocalPathTranslator — PathMapping 翻译 + 反向脱敏。

吸收 Poirot `sandbox/translators/local_path_translator.py`：
- translate_path 幂等；mask_output 是 translate_path 逆操作
- 按 container_path 长度降序匹配（防前缀误匹配）
- resolve() 跟随 symlink 后越界检测（防 symlink 穿越）
"""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path

from ..types import PathMapping, ResolvedPath


class LocalPathTranslator:
    def __init__(self, path_mappings: list[PathMapping]) -> None:
        self._mappings = path_mappings

    @cached_property
    def _mappings_by_container_specificity(self) -> list[PathMapping]:
        """按 container_path 长度降序（前向解析用）。"""
        return sorted(self._mappings, key=lambda m: len(m.container_path), reverse=True)

    @cached_property
    def _mappings_by_local_specificity(self) -> list[PathMapping]:
        """按 local_path 长度降序（反向解析用）。"""
        return sorted(self._mappings, key=lambda m: len(m.local_path), reverse=True)

    @cached_property
    def _resolved_local_paths(self) -> dict[PathMapping, str]:
        """每个 mapping 的 resolve() 后物理路径（避免重复 syscall）。"""
        return {m: str(Path(m.local_path).resolve()) for m in self._mappings}

    @cached_property
    def _command_pattern(self) -> re.Pattern[str] | None:
        """bash 命令里容器路径的匹配器（shell-aware 边界）。"""
        patterns = [
            re.escape(m.container_path) + r"(?=/|$|[\s\"';&|<>()])"
            for m in self._mappings_by_container_specificity
        ]
        return re.compile("|".join(f"({p})" for p in patterns)) if patterns else None

    @cached_property
    def _reverse_output_patterns(self) -> list[tuple[re.Pattern[str], PathMapping, str]]:
        """物理路径 → 虚拟路径的反向匹配（输出脱敏用），按 local_path 长度降序。"""
        result: list[tuple[re.Pattern[str], PathMapping, str]] = []
        for m in self._mappings_by_local_specificity:
            local = self._resolved_local_paths[m]
            escaped = re.escape(local)
            path_re = re.compile(escaped + r"(?:[\\/][^\\/\s\"';&|<>()]*)*")
            result.append((path_re, m, local))
        return result

    def _resolve_path_with_mapping(self, virtual_path: str) -> ResolvedPath:
        """虚拟路径 → ResolvedPath（含物理路径 + 匹配的 mapping）。"""
        for mapping in self._mappings_by_container_specificity:
            container_path = mapping.container_path.rstrip("/")
            if virtual_path == container_path or virtual_path.startswith(container_path + "/"):
                relative = virtual_path[len(container_path):].lstrip("/")
                local_root = Path(self._resolved_local_paths[mapping])
                resolved = (local_root / relative).resolve()
                try:
                    resolved.relative_to(local_root)
                except ValueError as exc:
                    raise PermissionError(f"path traversal detected: {virtual_path}") from exc
                return ResolvedPath(str(resolved), mapping)
        return ResolvedPath(virtual_path, None)

    def translate_path(self, virtual_path: str) -> str:
        return self._resolve_path_with_mapping(virtual_path).path

    def translate_command(self, command: str) -> str:
        if self._command_pattern is None:
            return command
        return self._command_pattern.sub(
            lambda m: self.translate_path(m.group(0)), command
        )

    def mask_output(self, output: str) -> str:
        """物理路径 → 虚拟路径（脱敏）。"""
        result = output
        for path_re, mapping, local in self._reverse_output_patterns:
            container = mapping.container_path

            def replace_match(match: re.Match[str], container=container, local=local) -> str:
                matched = match.group(0)
                relative = matched[len(local):].replace("\\", "/")
                return f"{container}{relative}"

            result = path_re.sub(replace_match, result)
        return result
