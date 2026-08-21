"""路径翻译契约（三组件之一）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PathTranslator(Protocol):
    """虚拟路径 ↔ 物理路径翻译契约。

    Local 用 PathMapping 翻译；Docker 用 Identity 直传。
    mask_output 归 PathTranslator（脱敏是路径翻译逆操作，SecurityGuard 不碰输出）。

    INVARIANT:
    - translate_path 幂等：同一虚拟路径多次翻译结果一致
    - mask_output 是 translate_path 逆操作：物理路径 → 虚拟路径
    """

    def translate_path(self, virtual_path: str) -> str:
        """虚拟路径 → 物理路径。Identity 实现直传。"""
        ...

    def translate_command(self, command: str) -> str:
        """bash 命令里的虚拟路径 → 物理路径。Identity 实现直传。"""
        ...

    def mask_output(self, output: str) -> str:
        """输出里的物理路径 → 虚拟路径（脱敏）。Identity 实现直传。"""
        ...
