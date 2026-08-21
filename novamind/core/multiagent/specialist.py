"""SpecialistAgent Protocol — 专业 agent 高层抽象（黑盒，自带 model）。"""

from __future__ import annotations

from typing import Protocol

from .types import SpecialistCapabilities, SpecialistRawResult, SpecialistRequest


class SpecialistAgent(Protocol):
    """专业 agent 契约（黑盒，自带 model + ReAct loop）。"""

    @property
    def name(self) -> str:
        ...

    @property
    def capabilities(self) -> SpecialistCapabilities:
        ...

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        """执行 specialist 调用，返 raw output。抛 SpecialistError 子类。"""
        ...
