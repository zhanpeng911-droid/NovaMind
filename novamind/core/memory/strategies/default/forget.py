"""Composite 遗忘策略（TTL + strength 两规则）。"""

from __future__ import annotations

from ...config import get_memory_config
from ...schema import MemoryTrace
from ._constants import FORGET_THRESHOLDS
from .decay import EbbinghausDecayPolicy


class CompositeForgetPolicy:
    """两规则遗忘：TTL 过期 或 strength 低于阈值。"""

    def __init__(self, decay_policy: EbbinghausDecayPolicy | None = None) -> None:
        self._decay_policy = decay_policy or EbbinghausDecayPolicy()

    def should_forget(self, trace: MemoryTrace, now: float) -> bool:
        thresholds = self._get_thresholds()

        last_access = trace.last_accessed if trace.last_accessed > 0 else trace.created_at
        ttl_seconds = thresholds["ttl_hours"] * 3600.0
        if (now - last_access) > ttl_seconds:
            return True

        current_strength = self._decay_policy.compute_strength(trace, now)
        if current_strength < thresholds["strength_threshold"]:
            return True

        return False

    def _get_thresholds(self) -> dict:
        config = get_memory_config()
        if hasattr(config, "forget") and config.forget:
            return {
                "strength_threshold": config.forget.get("strength_threshold", FORGET_THRESHOLDS["strength_threshold"]),
                "ttl_hours": config.forget.get("ttl_hours", FORGET_THRESHOLDS["ttl_hours"]),
                "conflict_window_hours": config.forget.get("conflict_window_hours", FORGET_THRESHOLDS["conflict_window_hours"]),
            }
        return FORGET_THRESHOLDS
