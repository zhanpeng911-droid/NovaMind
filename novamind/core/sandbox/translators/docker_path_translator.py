"""DockerPathTranslator — 容器内直传 + 反向映射到 host。

吸收 Poirot `sandbox/translators/docker_path_translator.py`：
- translate_path 直传（容器内挂载路径 = bind mount 物理路径）
- reverse_translate 反向映射（供 Sandbox.get_host_path 用，artifact 提取）
"""

from __future__ import annotations

from pathlib import Path

# Docker 容器内挂载区前缀（对齐 Poirot 的 /mnt/poirot/user-data，改为 novamind 命名）
VIRTUAL_PREFIX = "/mnt/novamind/user_data"


class DockerPathTranslator:
    """Docker 路径 translator：容器内直传 + 反向映射到 host。"""

    def __init__(self, sandbox_root: str | Path, sandbox_id: str) -> None:
        self._host_root = str(Path(sandbox_root) / sandbox_id).replace("\\", "/")

    def translate_path(self, virtual_path: str) -> str:
        return virtual_path

    def translate_command(self, command: str) -> str:
        return command

    def mask_output(self, output: str) -> str:
        return output

    def reverse_translate(self, virtual_path: str) -> str:
        """虚拟路径 → host 物理路径（Windows 或 Linux，取决于 sandbox_root）。

        /mnt/novamind/user_data/foo → <sandbox_root>/<sandbox_id>/foo
        非挂载区前缀抛 ValueError。输出统一正斜杠。
        """
        if virtual_path != VIRTUAL_PREFIX and not virtual_path.startswith(VIRTUAL_PREFIX + "/"):
            raise ValueError(f"path not under {VIRTUAL_PREFIX}: {virtual_path}")
        relative = virtual_path[len(VIRTUAL_PREFIX):].lstrip("/")
        return f"{self._host_root}/{relative}" if relative else self._host_root
