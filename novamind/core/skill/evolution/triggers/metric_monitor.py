"""MetricMonitorTrigger — 周期 metric 扫描触发（effective_rate 跌破阈值）。"""

from __future__ import annotations

from typing import Any

from ..types import EvolutionContext
from ...types import SkillRecord

_FALLBACK_THRESHOLD = 0.4
_LOW_COMPLETION_THRESHOLD = 0.35
_HIGH_APPLIED_FOR_FIX = 0.4


class MetricMonitorTrigger:
    def __init__(self, threshold: float = 0.3, min_selections: int = 5, cooldown_turns: int = 10, llm: Any | None = None) -> None:
        self._threshold = threshold
        self._min_selections = min_selections
        self._cooldown_turns = cooldown_turns
        self._llm = llm
        self._last_evolve_selections: dict[str, int] = {}

    def should_trigger(self, store: Any) -> list[EvolutionContext]:
        results: list[EvolutionContext] = []
        for rec in store.list_active():
            if not rec.enabled:
                continue
            if rec.total_selections < self._min_selections:
                continue
            last = self._last_evolve_selections.get(rec.name, 0)
            if rec.total_selections - last < self._cooldown_turns:
                continue

            direction = self._diagnose_skill_health(rec)
            if direction is None:
                continue
            if self._llm is not None and not self._llm_confirm_evolution(rec, direction):
                continue

            results.append(EvolutionContext(trigger="METRIC", evolution_type="FIX", target_skill=rec, fix_direction=direction))
        return results

    def mark_evolved(self, skill_name: str, total_selections: int) -> None:
        self._last_evolve_selections[skill_name] = total_selections

    @staticmethod
    def _diagnose_skill_health(record: SkillRecord) -> str | None:
        if record.fallback_rate > _FALLBACK_THRESHOLD:
            return f"高 fallback_rate({record.fallback_rate:.0%})：skill 常被选但未应用，指令不清或过时。"
        if record.applied_rate > _HIGH_APPLIED_FOR_FIX and record.completion_rate < _LOW_COMPLETION_THRESHOLD:
            return f"低 completion_rate({record.completion_rate:.0%}) 但高 applied_rate({record.applied_rate:.0%})：指令可能错或不全。"
        return None

    def _llm_confirm_evolution(self, record: SkillRecord, direction: str) -> bool:
        try:
            from langchain_core.messages import HumanMessage

            prompt = (
                f"Skill: {record.name}\n描述: {record.description}\n诊断: {direction}\n"
                f"metrics: selections={record.total_selections}, effective_rate={record.effective_rate:.0%}\n"
                f"这 skill 真需要进化吗？只返 yes 或 no。"
            )
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content if hasattr(resp, "content") else str(resp)
            return "yes" in content.lower()
        except Exception:
            return False
