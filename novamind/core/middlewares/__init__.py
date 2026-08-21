"""横切中间件包（复数名，避开旧 novamind.core.middleware 洋葱管道）。

P0 阶段只定义协议 + 管理器；具体中间件（记忆/技能/沙箱/上下文治理等）在后续阶段挂载。
"""

from .protocol import BaseAgentMiddleware, MiddlewareContext, MiddlewareResult
from .manager import MiddlewareManager

__all__ = [
    "BaseAgentMiddleware",
    "MiddlewareContext",
    "MiddlewareResult",
    "MiddlewareManager",
]
