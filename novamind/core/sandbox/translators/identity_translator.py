"""Identity 路径翻译器。Docker 用，虚拟路径直传无翻译。"""

from __future__ import annotations


class IdentityTranslator:
    """Identity 路径翻译器。Docker bind mount 物理对齐，不需翻译。"""

    def translate_path(self, virtual_path: str) -> str:
        return virtual_path

    def translate_command(self, command: str) -> str:
        return command

    def mask_output(self, output: str) -> str:
        return output
