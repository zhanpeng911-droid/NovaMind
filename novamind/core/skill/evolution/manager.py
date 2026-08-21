"""EvolutionManager — 自进化闭环编排。

run_cycle：扫所有 trigger → focus → mutate → eval → gate → create_version/record。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .types import EvalContext, EvolutionContext, EvolutionRecord
from ..types import SkillRecord


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvolutionManager:
    """编排：trigger → focus → mutate → eval → gate → create_version/record。"""

    def __init__(
        self,
        store: Any,
        triggers: list[Any],
        focuser: Any,
        mutator: Any,
        eval_bridge: Any,
        gate: Any,
        llm: Any | None = None,
        journal: Any | None = None,
    ) -> None:
        self._store = store
        self._triggers = triggers
        self._focuser = focuser
        self._mutator = mutator
        self._eval_bridge = eval_bridge
        self._gate = gate
        self._llm = llm
        self._journal = journal

    def run_cycle(self) -> list[EvolutionRecord]:
        records: list[EvolutionRecord] = []
        for trigger in self._triggers:
            contexts = trigger.should_trigger(self._store)
            for ctx in contexts:
                rec = self._run_evolution(ctx)
                if rec is not None:
                    records.append(rec)
                if ctx.target_skill is not None and hasattr(trigger, "mark_evolved"):
                    trigger.mark_evolved(ctx.target_skill.name, ctx.target_skill.total_selections)
        return records

    def evolve_skill(self, skill_name: str) -> EvolutionRecord:
        rec = self._store.get_active(skill_name)
        if rec is None:
            raise ValueError(f"skill not found: {skill_name}")
        ctx = EvolutionContext(trigger="METRIC", evolution_type="FIX", target_skill=rec, fix_direction="手动触发进化")
        result = self._run_evolution(ctx)
        if result is None:
            raise RuntimeError("evolution produced no record")
        return result

    def capture_skill(self, pattern: str, suggested_name: str) -> EvolutionRecord:
        ctx = EvolutionContext(
            trigger="CAPTURE", evolution_type="CAPTURED", target_skill=None,
            capture_pattern=pattern, suggested_name=suggested_name,
        )
        result = self._run_evolution(ctx)
        if result is None:
            raise RuntimeError("capture produced no record")
        return result

    def _run_evolution(self, ctx: EvolutionContext) -> EvolutionRecord | None:
        ctx = self._focuser.focus(ctx, self._store)
        candidate, diff = self._mutator.mutate(ctx, self._llm)
        baseline = ctx.target_skill
        metrics_baseline = None
        if baseline is not None:
            try:
                metrics_baseline = self._store.get_metrics(baseline.skill_id)
            except Exception:
                metrics_baseline = None
        eval_ctx = EvalContext(
            baseline=baseline if baseline is not None else candidate,
            candidate=candidate,
            metrics_baseline=metrics_baseline,
        )
        eval_result = self._eval_bridge.evaluate(eval_ctx)
        decision = self._gate.decide(candidate, baseline, eval_result)

        created_id: str | None = None
        if decision.recommendation in ("accept", "accept_new_best"):
            parent_id = baseline.skill_id if baseline is not None else ""
            try:
                created_id = self._store.create_version(parent_id, candidate, candidate.lineage.origin)
            except Exception:
                created_id = None

        rec = EvolutionRecord(
            evolution_id=f"evo_{uuid.uuid4().hex[:12]}",
            skill_name=candidate.name,
            evolution_type=ctx.evolution_type,
            trigger=ctx.trigger,
            baseline_id=baseline.skill_id if baseline is not None else None,
            candidate_id=candidate.skill_id,
            failure_focus=ctx.fix_direction or ctx.capture_pattern,
            mutation_diff=diff,
            eval_score=eval_result.score,
            gate_decision=decision.recommendation,
            created_version_id=created_id,
            timestamp=_now_iso(),
        )
        try:
            self._store.record_evolution(rec)
        except Exception:
            pass
        return rec
