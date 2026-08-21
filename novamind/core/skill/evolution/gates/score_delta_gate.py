"""ScoreDeltaGate — 基础门（零 LLM，用 EvalResult）。"""

from __future__ import annotations

from ..types import EvalResult, GateDecision
from ...types import SkillRecord


class ScoreDeltaGate:
    def __init__(self, min_delta: float = 0.0) -> None:
        self._min_delta = min_delta

    def decide(self, candidate: SkillRecord, baseline: SkillRecord, eval_result: EvalResult) -> GateDecision:
        if eval_result.hard_failures:
            return GateDecision(recommendation="reject", reason=f"hard_failures: {eval_result.hard_failures}")
        if baseline is None:
            if eval_result.score > 0:
                return GateDecision(recommendation="accept", reason=f"CAPTURED score={eval_result.score:.2f}", new_version_id=candidate.skill_id)
            return GateDecision(recommendation="reject", reason="CAPTURED score=0")
        baseline_score = self._baseline_score(eval_result)
        if eval_result.score > baseline_score + self._min_delta:
            return GateDecision(
                recommendation="accept",
                reason=f"candidate={eval_result.score:.2f} > baseline={baseline_score:.2f} + delta={self._min_delta}",
                new_version_id=candidate.skill_id,
            )
        return GateDecision(
            recommendation="reject",
            reason=f"candidate={eval_result.score:.2f} <= baseline={baseline_score:.2f} + delta={self._min_delta}",
        )

    @staticmethod
    def _baseline_score(eval_result: EvalResult) -> float:
        if not eval_result.evidence:
            return 0.0
        passed = sum(1 for e in eval_result.evidence if e.baseline_pass)
        return passed / len(eval_result.evidence)
