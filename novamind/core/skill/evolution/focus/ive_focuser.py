"""IVEFocuser — IVE 5 问失败聚焦（区分 fundamental vs implementation）。"""

from __future__ import annotations

from typing import Any

from ..types import EvolutionContext, FailureClass, FailureEvidence


class IVEFocuser:
    def __init__(self, llm: Any | None = None, impl_fail_threshold: int = 3) -> None:
        self._llm = llm
        self._impl_fail_threshold = impl_fail_threshold
        self._impl_fail_counts: dict[str, int] = {}

    def focus(self, ctx: EvolutionContext, store: Any) -> EvolutionContext:
        if not ctx.failure_evidence:
            return ctx

        judgment_notes = self._read_judgment_notes(ctx, store)

        if self._llm is None:
            return self._degrade_focus(ctx, judgment_notes)

        skill_name = ctx.target_skill.name if ctx.target_skill else ctx.suggested_name
        failure_class, fix_direction = self._llm_diagnose(ctx, skill_name, judgment_notes)

        if failure_class == "IMPLEMENTATION":
            self._impl_fail_counts[skill_name] = self._impl_fail_counts.get(skill_name, 0) + 1
            if self._impl_fail_counts[skill_name] >= self._impl_fail_threshold:
                failure_class = "FUNDAMENTAL"
                fix_direction = f"[升级 fundamental] implementation 累计 {self._impl_fail_counts[skill_name]} 次。 {fix_direction}"
        elif failure_class == "FUNDAMENTAL":
            self._impl_fail_counts[skill_name] = 0

        from dataclasses import replace

        updated_evidence = tuple(
            FailureEvidence(
                turn_index=e.turn_index, tool_name=e.tool_name,
                failure_class=failure_class, description=e.description,
                impl_fail_count=self._impl_fail_counts.get(skill_name, 0),
            )
            for e in ctx.failure_evidence
        )
        return replace(ctx, failure_evidence=updated_evidence, fix_direction=fix_direction)

    def _degrade_focus(self, ctx: EvolutionContext, judgment_notes: list[str] | None = None) -> EvolutionContext:
        from dataclasses import replace

        summaries = [e.description for e in ctx.failure_evidence]
        parts = ["失败证据：" + " | ".join(summaries)]
        if judgment_notes:
            parts.append("SkillJudgment 偏差：" + " | ".join(judgment_notes))
        fix_direction = "LLM 未启用，降级全量摘要。" + " ".join(parts)
        return replace(ctx, fix_direction=fix_direction)

    @staticmethod
    def _read_judgment_notes(ctx: EvolutionContext, store: Any) -> list[str]:
        if store is None or ctx.target_skill is None:
            return []
        try:
            judgments = store.get_judgments(ctx.target_skill.skill_id, limit=10)
            return [j.deviation_note for j in judgments if j.deviation_note]
        except Exception:
            return []

    def _llm_diagnose(self, ctx: EvolutionContext, skill_name: str, judgment_notes: list[str] | None = None) -> tuple[FailureClass, str]:
        try:
            import json

            from langchain_core.messages import HumanMessage

            skill_desc = ctx.target_skill.description if ctx.target_skill else ctx.capture_pattern
            evidence_text = "\n".join(f"- turn={e.turn_index} tool={e.tool_name}: {e.description}" for e in ctx.failure_evidence)
            judgment_text = ""
            if judgment_notes:
                judgment_text = "\n\nSkillJudgment 偏差记录:\n" + "\n".join(f"- {n}" for n in judgment_notes)
            prompt = (
                f"Skill: {skill_name}\n描述: {skill_desc}\n\n失败证据:\n{evidence_text}{judgment_text}\n\n"
                f"IVE 5 问诊断，判断是 FUNDAMENTAL（指令错）还是 IMPLEMENTATION（执行偏差）。\n"
                f'只返 JSON: {{"class": "FUNDAMENTAL"|"IMPLEMENTATION", "direction": "修复方向"}}'
            )
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content if hasattr(resp, "content") else str(resp)
            s = content.find("{")
            e_idx = content.rfind("}")
            if s != -1 and e_idx != -1 and e_idx > s:
                data = json.loads(content[s : e_idx + 1])
                cls = data.get("class", "IMPLEMENTATION")
                if cls not in ("FUNDAMENTAL", "IMPLEMENTATION"):
                    cls = "IMPLEMENTATION"
                return cls, data.get("direction", "")
            return "IMPLEMENTATION", ""
        except Exception:
            return "IMPLEMENTATION", ""
