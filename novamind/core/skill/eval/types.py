"""Skill eval 评估层值对象 — frozen dataclass + Literal 枚举。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EvalLayer = Literal["execution", "task", "response"]
Trend = Literal["improving", "stable", "degrading", "insufficient_data"]
ContractRuleKind = Literal["programmatic", "llm_binary"]


@dataclass(frozen=True)
class SkillJudgment:
    judgment_id: str
    skill_id: str
    skill_name: str
    task_id: str
    skill_applied: bool
    deviation_note: str = ""
    timestamp: str = ""


@dataclass(frozen=True)
class TaskQualityScore:
    """任务层 4 维加权评分（0.50 completion / 0.35 quality / 0.05 efficiency / 0.10 tool）。"""

    score_id: str
    task_id: str
    task_completion: float
    response_quality: float
    efficiency: float
    tool_usage: float
    overall_score: float
    rationale: str = ""
    timestamp: str = ""


@dataclass(frozen=True)
class ContractRule:
    rule_id: str
    kind: ContractRuleKind
    hard: bool
    description: str = ""
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SkillHealthReport:
    skill_id: str
    skill_name: str
    window_selections: int
    applied_rate: float
    completion_rate: float
    effective_rate: float
    fallback_rate: float
    trend: Trend
    recent_judgments: tuple[SkillJudgment, ...] = ()
    advice: str = ""


@dataclass(frozen=True)
class EvalRun:
    eval_run_id: str
    eval_layer: EvalLayer
    skill_ids: tuple[str, ...]
    candidate_id: str | None = None
    baseline_id: str | None = None
    result_json: str = ""
    timestamp: str = ""
