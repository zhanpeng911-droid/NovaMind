"""DockerPathGuard — Docker 写入路径白名单。

吸收 Poirot `sandbox/guards/docker_path_guard.py`：写入必须落挂载区，防止 LLM 写进
容器内部 /tmp 被 --rm 丢弃。bash 重定向目标（绝对路径）必须在挂载区。
"""

from __future__ import annotations

import re

from ..exceptions import SandboxPermissionError
from ..translators.docker_path_translator import VIRTUAL_PREFIX

_VIRTUAL_PREFIX_SLASH = VIRTUAL_PREFIX + "/"
_REDIRECT_PATTERN = re.compile(r'>{1,2}\s*(/[^\s;|&]*)')


class DockerPathGuard:
    def validate_path(self, path: str, *, write: bool = False) -> None:
        if not write:
            return
        if not path.startswith(_VIRTUAL_PREFIX_SLASH):
            raise SandboxPermissionError(
                f"write path must be under {_VIRTUAL_PREFIX_SLASH}: {path}",
                path=path,
                operation="validate",
            )

    def validate_command(self, command: str) -> None:
        for match in _REDIRECT_PATTERN.finditer(command):
            target = match.group(1)
            if not target.startswith(_VIRTUAL_PREFIX_SLASH):
                raise SandboxPermissionError(
                    f"bash redirect target must be under {_VIRTUAL_PREFIX_SLASH}: {target}",
                    path=target,
                    operation="validate_command",
                )
