"""SubagentProvider Protocol — self-copy subagent 契约。"""

from __future__ import annotations

from typing import Protocol

from .types import SubagentRequest, SubagentResult


class SubagentProvider(Protocol):
    """self-copy subagent 契约（leaf role，shared thread sandbox）。

    - leaf role：子 agent tool_groups 不含 multiagent，看不到 delegate_to_* tool
    - isolated context：全新状态，只传 goal + context_summary
    - shared thread sandbox：复用父 sandbox_id
    """

    def spawn(self, request: SubagentRequest) -> SubagentResult:
        """spawn 一个 self-copy subagent 执行任务。抛 SubagentError 子类。"""
        ...
