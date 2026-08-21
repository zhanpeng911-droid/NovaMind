"""Skill 数据模型 — frozen dataclass + 4 rate property。

吸收 Poirot `skill/types.py`：内容在文件（path），SkillRecord 只存引用 + metrics。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillLineage:
    """skill 版本血缘。"""

    parent_skill_ids: tuple[str, ...] = ()
    generation: int = 0
    origin: str = "IMPORTED"
    version_hash: str = ""
    created_by: str | None = None


@dataclass(frozen=True)
class SkillRecord:
    """skill 注册条目。内容在文件，SQLite 只存引用 + metrics。"""

    skill_id: str
    name: str
    path: str
    content_hash: str
    is_active: bool = True
    lineage: SkillLineage = field(default_factory=SkillLineage)
    description: str = ""
    allowed_tools: tuple[str, ...] = ()
    enabled: bool = True
    total_selections: int = 0
    total_applied: int = 0
    total_completions: int = 0
    total_fallbacks: int = 0
    created_at: str = ""
    last_updated: str = ""

    @property
    def applied_rate(self) -> float:
        return self.total_applied / self.total_selections if self.total_selections else 0.0

    @property
    def completion_rate(self) -> float:
        return self.total_completions / self.total_applied if self.total_applied else 0.0

    @property
    def effective_rate(self) -> float:
        return self.total_completions / self.total_selections if self.total_selections else 0.0

    @property
    def fallback_rate(self) -> float:
        return self.total_fallbacks / self.total_selections if self.total_selections else 0.0


@dataclass(frozen=True)
class SkillMetrics:
    """skill quality metrics 快照。"""

    skill_id: str
    selections: int
    applied: int
    completions: int
    fallbacks: int
    applied_rate: float
    completion_rate: float
    effective_rate: float
    fallback_rate: float


@dataclass(frozen=True)
class SkillHealth:
    """skill 健康状态。degraded = effective_rate < threshold AND selections >= min。"""

    skill_id: str
    name: str
    effective_rate: float
    fallback_rate: float
    total_selections: int
    degraded: bool
