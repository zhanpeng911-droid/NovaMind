"""记忆配置：MemoryConfig + STARTUP_ONLY_FIELDS + 全局单例。

吸收 Poirot `memory/config.py`：
- runtime 可切：enable_recall/extract/token_budget/decay/forget/phase2 通过 set_memory_config() 整替
- STARTUP_ONLY 仅 4 字段：use/storage_path/vector_store/graph_store 换需重启
- frozen + dict 内部可变矛盾解决：dict 视为不可变，改参数 = 构造新 MemoryConfig + 整替

已敲定：storage_path 默认 `memory_traces`，相对路径收敛到统一数据根（不再跟随 CWD），
显式绝对路径仍可覆盖；旧版 `.novamind/memory` 相对 CWD 的数据在首次构建时一次性迁移。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryConfig:
    """长期记忆配置（frozen，runtime 切走整替）。"""

    use: str = ""                              # 主 Provider 实现类，空=禁用；默认 "default"
    storage_path: str = "memory_traces"      # Markdown 持久化根（truth source，总在；相对路径按统一数据根解析）
    enable_recall: bool = True                 # before_model 召回（L4）
    enable_extract: bool = False               # after_model 实时抽取（默认关，走 L5）
    token_budget: int = 2000                   # 召回注入 token 上限
    decay: dict[str, Any] = field(default_factory=lambda: {
        "episodic": {"base_strength": 0.7, "decay_rate": 0.1},
        "semantic": {"base_strength": 0.8, "decay_rate": 0.02},
        "procedural": {"base_strength": 0.9, "decay_rate": 0.005},
    })
    forget: dict[str, Any] = field(default_factory=lambda: {
        "strength_threshold": 0.1,
        "ttl_hours": 720,   # 30 天
    })
    phase2: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False,
        "trigger_every_n_turns": 10,
        "trigger_on_session_end": True,
    })
    vector_store: str = ""                     # 可选 Vector adapter（空=不启用）
    graph_store: str = ""                      # 可选 Graph adapter（空=不启用）


STARTUP_ONLY_FIELDS = frozenset({
    "use", "storage_path", "vector_store", "graph_store",
})

_memory_config: MemoryConfig = MemoryConfig()
_config_lock = threading.Lock()


def get_memory_config() -> MemoryConfig:
    with _config_lock:
        return _memory_config


def set_memory_config(config: MemoryConfig) -> None:
    global _memory_config
    with _config_lock:
        _memory_config = config
