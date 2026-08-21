"""三层自进化技能系统（吸收 Poirot）。

L1 基础（SQLite+DAG+四计数器）/ L2 进化（触发→诊断→变异→门槛→棘轮）/ L3 评估（执行判定+四维评分+契约检查）。
"""

from .types import SkillRecord, SkillMetrics, SkillLineage, SkillHealth
from .store import SQLiteSkillStore
from .parser import parse_skill_file
from .injector import build_injection_text
from .selector import SkillSelector

__all__ = [
    "SkillRecord",
    "SkillMetrics",
    "SkillLineage",
    "SkillHealth",
    "SQLiteSkillStore",
    "parse_skill_file",
    "build_injection_text",
    "SkillSelector",
]
