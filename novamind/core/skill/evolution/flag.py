"""进化开关（bootstrap 提醒用）。

技能进化 L2（变异）/ L3（评估）与多 Agent L2/L3 默认关闭（决策 3：保守，节省 LLM
成本）。启动时通过 ``evolution_notice()`` 提醒用户是否开启，由环境变量控制：

- ``NOVAMIND_SKILL_EVOLUTION``：技能 L2 自进化（默认 off）
- ``NOVAMIND_MULTIAGENT_EVOLUTION``：多 Agent L2/L3（默认 off）

取值不区分大小写：``1`` / ``on`` / ``true`` / ``yes`` 视为开启。
"""

from __future__ import annotations

import os

SKILL_EVOLUTION_ENV = "NOVAMIND_SKILL_EVOLUTION"
MULTIAGENT_EVOLUTION_ENV = "NOVAMIND_MULTIAGENT_EVOLUTION"

_TRUTHY = {"1", "on", "true", "yes"}


def _env_flag(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def skill_evolution_enabled() -> bool:
    """技能 L2 自进化是否开启。"""
    return _env_flag(SKILL_EVOLUTION_ENV)


def multiagent_evolution_enabled() -> bool:
    """多 Agent L2/L3 是否开启。"""
    return _env_flag(MULTIAGENT_EVOLUTION_ENV)


def evolution_notice() -> str:
    """生成启动提醒文本（若全部默认关闭则给出开启提示）。"""
    skill_on = skill_evolution_enabled()
    multi_on = multiagent_evolution_enabled()
    if skill_on and multi_on:
        return ""
    parts = [
        (
            "技能自进化 L2/L3 已开启"
            if skill_on
            else f"技能自进化 L2/L3 默认关闭（设置 {SKILL_EVOLUTION_ENV}=on 开启）"
        ),
        (
            "多 Agent L2/L3 已开启"
            if multi_on
            else f"多 Agent L2/L3 默认关闭（设置 {MULTIAGENT_EVOLUTION_ENV}=on 开启）"
        ),
    ]
    return " · ".join(parts)
