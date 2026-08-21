"""默认策略常量（衰减参数 / 遗忘阈值 / 检索权重 / 巩固参数 / 关联默认）。"""

from __future__ import annotations

DECAY_PARAMS = {
    "episodic": {"base_strength": 0.7, "decay_rate": 0.1},
    "semantic": {"base_strength": 0.8, "decay_rate": 0.02},
    "procedural": {"base_strength": 0.9, "decay_rate": 0.005},
}

RETRIEVAL_WEIGHTS = {"similarity": 0.7, "strength": 0.3}

DECAY_COEFFICIENTS = {"access_boost": 0.1, "importance_boost": 0.05}

FORGET_THRESHOLDS = {
    "strength_threshold": 0.1,
    "ttl_hours": 720,
    "conflict_window_hours": 24,
}

CONSOLIDATE_PARAMS = {
    "min_traces_to_consolidate": 2,
    "max_traces_to_consolidate": 10,
    "default_consolidated_type": "semantic",
    "default_importance_boost": 0.1,
}

ASSOCIATE_DEFAULTS = {
    "default_strength": 0.5,
    "default_type": "related",
    "max_associations_per_trace": 20,
}

BM25_PARAMS = {"k1": 1.5, "b": 0.75, "epsilon": 0.25}
