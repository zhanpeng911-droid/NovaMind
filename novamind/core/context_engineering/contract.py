"""上下文治理接入契约（简化版）。

吸收 Poirot `context_engineering/contract.py`：
- GovernanceContext：hook 级统一入参
- GovernanceResult：hook 级统一出参（state_patch/messages_patch/override/jump_to）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class GovernanceContext:
    """hook 级统一入参。策略按 hook 取所需字段。"""

    state: Mapping[str, Any]
    governance: dict[str, Any] | None
    config: Any
    token_counter: Callable[[list], int]
    runtime: Any
    hook: str
    messages: list | None = None
    tool_result: Any | None = None


@dataclass(frozen=True)
class GovernanceResult:
    """hook 级统一出参。

    - state_patch：写回 state（含 governance），持久
    - messages_patch：消息级操作（RemoveMessage / 替换）
    - override：wrap_tool_call 的工具结果覆盖
    - jump_to：跳转目标节点
    """

    state_patch: dict[str, Any] | None = None
    messages_patch: list | None = None
    override: Any = None
    jump_to: str | None = None
