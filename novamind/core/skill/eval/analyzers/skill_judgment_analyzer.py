"""SkillJudgmentAnalyzer — 执行层 eval（LLM 判定 skill 是否被应用 + 偏差）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..types import SkillJudgment


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SkillJudgmentAnalyzer:
    def __init__(self, llm: Any | None = None, store: Any = None) -> None:
        self._llm = llm
        self._store = store

    async def judge(self, skill_id: str, skill_name: str, task_id: str, trace: str) -> SkillJudgment | None:
        if self._llm is None:
            return None
        try:
            judgment = self._llm_judge(skill_id, skill_name, task_id, trace)
        except Exception:
            return None
        if judgment is not None and self._store is not None:
            try:
                self._store.save_judgment(judgment)
            except Exception:
                pass
        return judgment

    def _llm_judge(self, skill_id: str, skill_name: str, task_id: str, trace: str) -> SkillJudgment | None:
        from langchain_core.messages import HumanMessage

        prompt = (
            f"Skill: {skill_name}\n任务: {task_id}\n\n执行 trace:\n{(trace or '')[:80000]}\n\n"
            f"判断该 skill 的指导是否被实际应用。"
            f'只返 JSON: {{"applied": true/false, "deviation": "偏差说明（未应用时）"}}'
        )
        resp = self._llm.invoke([HumanMessage(content=prompt)])
        content = resp.content if hasattr(resp, "content") else str(resp)
        data = self._extract_json(content)
        if data is None:
            return None
        return SkillJudgment(
            judgment_id=f"judge_{uuid.uuid4().hex[:12]}",
            skill_id=skill_id, skill_name=skill_name, task_id=task_id,
            skill_applied=bool(data.get("applied", False)),
            deviation_note=data.get("deviation", ""),
            timestamp=_now_iso(),
        )

    @staticmethod
    def _extract_json(content: str) -> dict | None:
        s = content.find("{")
        e = content.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(content[s : e + 1])
            except Exception:
                return None
        return None
