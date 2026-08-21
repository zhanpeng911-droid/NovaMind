"""SandboxBinder — 沙箱绑定契约（共享 thread sandbox）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BoundSandbox:
    """specialist 沙箱绑定结果（shared thread sandbox，复用父 sandbox_id）。"""

    sandbox_id: str
    specialist_name: str


class SandboxBinder(Protocol):
    """沙箱绑定契约（specialist 调用前绑定 shared thread sandbox）。"""

    def bind(self, specialist_name: str, sandbox_id: str) -> BoundSandbox:
        """绑定 specialist 与 sandbox。复用传入 sandbox_id，不创建新沙箱。"""
        ...


class PerSubagentBinder:
    """复用 lead agent thread sandbox_id，不创建新沙箱（INV#3 shared thread sandbox）。"""

    def bind(self, specialist_name: str, sandbox_id: str) -> BoundSandbox:
        return BoundSandbox(sandbox_id=sandbox_id, specialist_name=specialist_name)
