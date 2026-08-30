"""默认运行时中间件栈：五层记忆（L4 召回 + L5 沉淀）与上下文治理。

缺陷#2 修复：此前 create_agent_app 默认 middlewares 为空，CLI/GUI 运行时
不加载 README 宣称的记忆/治理能力。本模块提供产品级默认栈，由 entry/main.py
与 webui/server.py 接线；内核 create_agent_app 保持精简（显式传参才启用）。
"""
from __future__ import annotations

from typing import Any

from ..memory.bootstrap import start_memory_worker
from ..memory.config import get_memory_config
from ..memory.strategies.default.strategy import build_default_provider
from ..context_engineering.strategies.default.strategy import DefaultStrategy
from .context_governance_middleware import ContextGovernanceMiddleware
from .memory_consolidation_middleware import MemoryConsolidationMiddleware
from .memory_recall_middleware import MemoryRecallMiddleware


def build_default_middlewares(llm: Any) -> list:
    """按运行配置装配默认中间件栈（记忆 + 治理）。llm 用于 L5 worker 抽取。"""
    cfg = get_memory_config()
    provider = build_default_provider()
    worker = start_memory_worker(provider.manager(), llm)

    middlewares: list = [
        MemoryRecallMiddleware(
            provider,
            enable_recall=cfg.enable_recall,
            enable_extract=cfg.enable_extract,
            token_budget=cfg.token_budget,
        ),
        MemoryConsolidationMiddleware(
            worker,
            trigger_every_n_turns=int(cfg.phase2.get("trigger_every_n_turns", 10)),
        ),
    ]
    if getattr(cfg, "use", "") != "off":
        middlewares.append(ContextGovernanceMiddleware(DefaultStrategy(
            params={
                "preserve_recent": 3,
                "snapshot_dir": ".novamind/snapshots",
                "externalize_dir": ".novamind/externalized",
            },
            model=llm,
        )))
    return middlewares
