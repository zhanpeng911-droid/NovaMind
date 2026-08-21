"""Multi-Agent 核心数据契约 — frozen dataclass。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SpecialistCapability(str, Enum):
    CODING = "coding"
    RESEARCH = "research"
    REVIEW = "review"
    PLANNING = "planning"


@dataclass(frozen=True)
class SpecialistCapabilities:
    capabilities: tuple[SpecialistCapability, ...] = ()

    def has(self, capability: SpecialistCapability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    artifact_type: str
    specialist_name: str
    description: str = ""
    created_at: str | None = None


@dataclass(frozen=True)
class SpecialistRequest:
    goal: str
    success_criteria: str
    context_summary: str
    sandbox_id: str | None
    artifacts_path: str | None
    max_steps: int = 50
    timeout_seconds: int = 600
    allowed_tools: tuple[str, ...] = ()
    skill_injection: str | None = None


@dataclass(frozen=True)
class SpecialistRawResult:
    raw_output: str
    artifacts: tuple[ArtifactRef, ...] = ()
    usage: TokenUsage | None = None
    duration_seconds: float = 0.0
    exit_code: int = 0


@dataclass(frozen=True)
class SpecialistResult:
    specialist_name: str
    summary: str
    artifacts: tuple[ArtifactRef, ...] = ()
    success: bool = False
    gap_analysis: str = ""
    usage: TokenUsage | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    failure_category: str | None = None


@dataclass(frozen=True)
class SubagentRequest:
    goal: str
    success_criteria: str
    context_summary: str
    sandbox_id: str | None
    artifacts_path: str | None
    max_steps: int = 20
    timeout_seconds: int = 300
    allowed_tools: tuple[str, ...] = ()
    skill_injection: str | None = None


@dataclass(frozen=True)
class SubagentResult:
    summary: str
    artifacts: tuple[ArtifactRef, ...] = ()
    success: bool = False
    gap_analysis: str = ""
    usage: TokenUsage | None = None
    duration_seconds: float = 0.0
    error: str | None = None
