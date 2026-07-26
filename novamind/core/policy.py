"""
NovaMind lightweight harness policy layer.

Phase two keeps the format intentionally small and machine-checkable:
  - default allowed tools
  - per-tool read/write/confirmation metadata
  - simple user-input heuristics that trigger confirmation requirements
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .config import POLICY_PATH


DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "default_allowed_tools": [
        "get_current_time",
        "calculator",
        "get_system_model_info",
        "list_scheduled_tasks",
        "schedule_task",
        "delete_scheduled_task",
        "modify_scheduled_task",
        "save_user_profile",
        "list_office_files",
        "read_office_file",
        "write_office_file",
        "execute_office_shell",
    ],
    "tool_policies": {
        "read_office_file": {"mode": "read"},
        "list_office_files": {"mode": "read"},
        "write_office_file": {
            "mode": "write",
            "confirmation_keywords": ["删除", "批量", "覆盖全部", "rewrite all", "overwrite all"],
        },
        "execute_office_shell": {
            "mode": "execute",
            "confirmation_keywords": ["删除", "批量", "覆盖全部", "remove", "delete", "overwrite all"],
        },
    },
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False

    def as_log_payload(self, tool_name: str) -> dict[str, Any]:
        return {
            "tool": tool_name,
            "allowed": self.allowed,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
        }


class HarnessPolicy:
    def __init__(self, raw_policy: dict[str, Any]):
        self._raw = raw_policy
        self._default_allowed_tools = set(raw_policy.get("default_allowed_tools", []))
        self._tool_policies = raw_policy.get("tool_policies", {})

    @classmethod
    def load(cls, policy_path: str = POLICY_PATH) -> "HarnessPolicy":
        if not os.path.exists(policy_path):
            return cls(DEFAULT_POLICY)

        with open(policy_path, "r", encoding="utf-8", errors="ignore") as f:
            loaded = json.load(f)
        return cls(loaded)

    def evaluate_tool_call(
        self,
        tool_name: str,
        latest_user_input: str = "",
        tool_args: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        tool_args = tool_args or {}
        if tool_name not in self._default_allowed_tools:
            return PolicyDecision(
                allowed=False,
                reason="tool_not_allowed_by_policy",
            )

        tool_policy = self._tool_policies.get(tool_name, {})
        confirmation_keywords = [k.lower() for k in tool_policy.get("confirmation_keywords", [])]
        normalized_input = (latest_user_input or "").lower()
        requires_confirmation = any(keyword in normalized_input for keyword in confirmation_keywords)

        if requires_confirmation:
            return PolicyDecision(
                allowed=False,
                reason="confirmation_required_by_policy",
                requires_confirmation=True,
            )

        return PolicyDecision(
            allowed=True,
            reason="allowed_by_policy",
        )

    def describe(self) -> dict[str, Any]:
        return self._raw
