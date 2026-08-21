"""GitRatchet — 上线后兜底回滚（退化自动切回旧版）。"""

from __future__ import annotations

from typing import Any

from ...types import SkillRecord


class GitRatchet:
    def __init__(self, degradation_threshold: float = 0.3, min_selections: int = 5, runtime_tracker: Any | None = None) -> None:
        self._threshold = degradation_threshold
        self._min_selections = min_selections
        self._runtime_tracker = runtime_tracker

    def check_and_rollback(self, store: Any, current: SkillRecord) -> str | None:
        if current.total_selections < self._min_selections:
            return None
        if current.effective_rate >= self._threshold:
            return None

        versions = store.get_versions(current.name)
        parent_ids = current.lineage.parent_skill_ids
        rollback_target = None
        for v in versions:
            if v.skill_id == current.skill_id:
                continue
            if v.skill_id in parent_ids:
                rollback_target = v
                break
        if rollback_target is None and versions:
            others = [v for v in versions if v.skill_id != current.skill_id]
            if others:
                rollback_target = min(others, key=lambda v: v.lineage.generation)

        if rollback_target is None:
            return None

        store.rollback(rollback_target.skill_id)
        return rollback_target.skill_id
