"""Skill 自进化层（L2：触发→诊断→变异→门槛→棘轮）。"""

from .manager import EvolutionManager
from .types import EvolutionContext, EvolutionRecord, EvalContext, EvalResult, GateDecision
from .focus.ive_focuser import IVEFocuser
from .mutators.llm_mutator import LLMMutator
from .gates.score_delta_gate import ScoreDeltaGate
from .gates.git_ratchet import GitRatchet
from .triggers.metric_monitor import MetricMonitorTrigger
from .eval.programmatic_bridge import ProgrammaticEvalBridge
from .flag import (
    MULTIAGENT_EVOLUTION_ENV,
    SKILL_EVOLUTION_ENV,
    evolution_notice,
    multiagent_evolution_enabled,
    skill_evolution_enabled,
)

__all__ = [
    "EvolutionManager",
    "EvolutionContext",
    "EvolutionRecord",
    "EvalContext",
    "EvalResult",
    "GateDecision",
    "IVEFocuser",
    "LLMMutator",
    "ScoreDeltaGate",
    "GitRatchet",
    "MetricMonitorTrigger",
    "ProgrammaticEvalBridge",
    "SKILL_EVOLUTION_ENV",
    "MULTIAGENT_EVOLUTION_ENV",
    "evolution_notice",
    "skill_evolution_enabled",
    "multiagent_evolution_enabled",
]
