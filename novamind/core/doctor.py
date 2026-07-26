"""
NovaMind entropy-management checks.

This module implements a lightweight "doctor" lane that audits:
  - required structured docs presence
  - harness policy validity
  - tool coverage between runtime docs and policy allowlist
  - recent policy violations visible in audit logs
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .config import DOCS_DIR, LOG_DIR, POLICY_PATH
from .policy import HarnessPolicy
from .tools.builtins import BUILTIN_TOOLS


REQUIRED_DOCS = [
    "INDEX.md",
    "runtime-overview.md",
    "sandbox-policy.md",
    "tool-contracts.md",
    "session-model.md",
    os.path.join("playbooks", "file-edit.md"),
]


@dataclass(frozen=True)
class DoctorFinding:
    level: str
    code: str
    message: str
    suggestion: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class DoctorReport:
    findings: tuple[DoctorFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def counts(self) -> dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for finding in self.findings:
            counts[finding.level] = counts.get(finding.level, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": self.counts(),
            "findings": [finding.as_dict() for finding in self.findings],
        }


def run_doctor() -> DoctorReport:
    findings: list[DoctorFinding] = []
    findings.extend(_check_required_docs())
    findings.extend(_check_policy_file())
    findings.extend(_check_policy_tool_coverage())
    findings.extend(_check_recent_policy_violations())
    if not findings:
        findings.append(DoctorFinding(
            "info",
            "doctor_clean",
            "No issues detected.",
            "Keep docs, policies, and logs under review as the tool surface evolves.",
        ))
    return DoctorReport(findings=tuple(findings))


def _check_required_docs() -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    for relative_path in REQUIRED_DOCS:
        full_path = os.path.join(DOCS_DIR, relative_path)
        if not os.path.exists(full_path):
            findings.append(DoctorFinding(
                "error",
                "missing_doc",
                f"Missing structured doc: {relative_path}",
                f"Create {relative_path} under docs/ and link it from INDEX.md.",
            ))
    return findings


def _check_policy_file() -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    if not os.path.exists(POLICY_PATH):
        findings.append(DoctorFinding(
            "warning",
            "missing_policy_file",
            "Harness policy file is missing; runtime will fall back to defaults.",
            "Add harness/policies.json so tool constraints stay explicit and reviewable.",
        ))
        return findings

    try:
        with open(POLICY_PATH, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
    except Exception as exc:
        findings.append(DoctorFinding(
            "error",
            "invalid_policy_json",
            f"Harness policy file is not valid JSON: {exc}",
            "Fix the JSON syntax in harness/policies.json and rerun novamind doctor.",
        ))
        return findings

    if "default_allowed_tools" not in data:
        findings.append(DoctorFinding(
            "error",
            "policy_missing_allowlist",
            "Harness policy file is missing default_allowed_tools.",
            "Add a default_allowed_tools array so policy coverage is machine-checkable.",
        ))
    return findings


def _check_policy_tool_coverage() -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    policy = HarnessPolicy.load()
    policy_tools = set(policy.describe().get("default_allowed_tools", []))
    builtin_tools = {tool.name for tool in BUILTIN_TOOLS}

    undocumented_policy_tools = sorted(policy_tools - builtin_tools)
    if undocumented_policy_tools:
        findings.append(DoctorFinding(
            "warning",
            "policy_unknown_tools",
            "Policy references unknown tools: " + ", ".join(undocumented_policy_tools),
            "Remove stale tool names from harness/policies.json or add the missing tool implementations.",
        ))

    uncovered_builtin_tools = sorted(builtin_tools - policy_tools)
    if uncovered_builtin_tools:
        findings.append(DoctorFinding(
            "warning",
            "policy_missing_builtin_tools",
            "Builtin tools not present in policy allowlist: " + ", ".join(uncovered_builtin_tools),
            "Decide whether to add these builtin tools to harness/policies.json or intentionally block them.",
        ))

    tool_contract_path = os.path.join(DOCS_DIR, "tool-contracts.md")
    if os.path.exists(tool_contract_path):
        with open(tool_contract_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if "Tool usage rules" not in content:
            findings.append(DoctorFinding(
                "warning",
                "tool_contracts_thin",
                "tool-contracts.md is missing the 'Tool usage rules' section.",
                "Document the expected tool-selection rules in docs/tool-contracts.md.",
            ))

    return findings


def _check_recent_policy_violations(limit: int = 5) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    if not os.path.isdir(LOG_DIR):
        return findings

    violation_count = 0
    for entry in sorted(os.scandir(LOG_DIR), key=lambda e: e.stat().st_mtime, reverse=True):
        if not entry.is_file() or not entry.name.endswith(".jsonl"):
            continue
        try:
            with open(entry.path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    if data.get("event") == "policy_violation":
                        violation_count += 1
        except OSError:
            continue
        if violation_count >= limit:
            break

    if violation_count:
        findings.append(DoctorFinding(
            "info",
            "recent_policy_violations",
            f"Detected {violation_count} recent policy_violation events in audit logs.",
            "Review the recent blocked actions to decide whether docs, policies, or user flows need adjustment.",
        ))
    return findings
