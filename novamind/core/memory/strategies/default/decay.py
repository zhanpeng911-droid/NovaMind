"""Ebbinghaus 衰减策略（lazy decay）。

strength = base_strength × (1 - decay_rate)^time_hours + log(1+access_count)×0.1 + importance×0.05
lazy decay：compute_strength 不修改 trace，retrieve 时调用 + with_strength 更新。strength 钳制 [0,1]。
"""

from __future__ import annotations

import math

from ...config import get_memory_config
from ...schema import MemoryTrace, MemoryType
from ._constants import DECAY_COEFFICIENTS, DECAY_PARAMS


class EbbinghausDecayPolicy:
    """Ebbinghaus 衰减策略。纯计算，线程安全。"""

    def compute_strength(self, trace: MemoryTrace, now: float) -> float:
        params = self._get_decay_params(trace.type)
        base_strength = params["base_strength"]
        decay_rate = params["decay_rate"]

        if trace.last_accessed <= 0:
            time_hours = max(0.0, (now - trace.created_at) / 3600.0)
        else:
            time_hours = max(0.0, (now - trace.last_accessed) / 3600.0)

        decayed_strength = base_strength * ((1.0 - decay_rate) ** time_hours)
        access_boost = math.log(1 + trace.access_count) * DECAY_COEFFICIENTS["access_boost"]
        importance_boost = trace.importance * DECAY_COEFFICIENTS["importance_boost"]

        strength = decayed_strength + access_boost + importance_boost
        return max(0.0, min(1.0, strength))

    def _get_decay_params(self, type: MemoryType) -> dict:
        config = get_memory_config()
        type_key = type.value if isinstance(type, MemoryType) else str(type)
        if hasattr(config, "decay") and type_key in config.decay:
            return config.decay[type_key]
        return DECAY_PARAMS[type_key]
