"""五层长期记忆系统（吸收 Poirot）。

L1 schema / L2 衰减遗忘 / L3 store+retriever / L4 中间件注入 / L5 后台沉淀。
"""

from .schema import MemoryTrace, MemoryType, Association, OperationLog
from .config import MemoryConfig, get_memory_config, set_memory_config
from .types import MemoryQuery, MemoryFilter, RetrievalResult
from .exceptions import MemoryError, MemoryNotFoundError, MemoryConflictError
from .strategies.default.strategy import DefaultMemoryProvider, build_default_provider

__all__ = [
    "MemoryTrace",
    "MemoryType",
    "Association",
    "OperationLog",
    "MemoryConfig",
    "get_memory_config",
    "set_memory_config",
    "MemoryQuery",
    "MemoryFilter",
    "RetrievalResult",
    "MemoryError",
    "MemoryNotFoundError",
    "MemoryConflictError",
    "DefaultMemoryProvider",
    "build_default_provider",
]
