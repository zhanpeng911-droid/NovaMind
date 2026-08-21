"""上下文治理默认常量（P0-P5 阈值 + 时区）。"""

from __future__ import annotations

from datetime import timezone, timedelta

# 中国标准时间（东八区）
CST = timezone(timedelta(hours=8))

# P0-P5 分段优先级阈值（fraction 占真实窗口比例）
DEFAULT_THRESHOLDS: dict[str, float] = {
    "p1_externalize": 0.40,
    "p2_thinking": 0.50,
    "p3_observations": 0.60,
    "p4_summarize": 0.80,
    "p5_stop_toolcall": 0.90,
    "hard_stop": 0.99,
}
