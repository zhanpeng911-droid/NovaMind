"""
NovaMind 中间件管道

提供请求/响应级别的拦截器，用于：
  - Token用量追踪与成本估算
  - 请求速率限制
  - 审计日志记录
  - 请求预处理/后处理

设计模式：类似洋葱模型，请求从外层进入，响应从内层返回。
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class MiddlewareContext:
    """
    中间件上下文：在管道中传递的请求/响应数据

    attributes: 自由扩展字段，各中间件可以往里面塞自己的数据
    """
    messages: list = field(default_factory=list)
    thread_id: str = "default"
    provider: str = ""
    model: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


# 中间件函数签名：(context, next) -> response
MiddlewareFunc = Callable[[MiddlewareContext, Callable], Awaitable[Any]]


class MiddlewarePipeline:
    """
    中间件管道

    用法：
        pipeline = MiddlewarePipeline()
        pipeline.add(my_logging_middleware)
        pipeline.add(my_token_tracker)

        result = await pipeline.execute(ctx, actual_handler)
    """

    def __init__(self):
        self._middlewares: list[MiddlewareFunc] = []

    def add(self, middleware: MiddlewareFunc) -> None:
        """注册一个中间件"""
        self._middlewares.append(middleware)

    def remove(self, middleware: MiddlewareFunc) -> None:
        """移除一个中间件"""
        self._middlewares = [m for m in self._middlewares if m is not middleware]

    async def execute(
        self,
        context: MiddlewareContext,
        handler: Callable[[MiddlewareContext], Awaitable[Any]],
    ) -> Any:
        """
        执行中间件管道

        请求从外层向内层传递，最终到达 handler。
        响应从内层向外层返回。
        """
        # 从最内层开始构建调用链
        chain = handler

        # 反向包装：最后一个中间件最先执行
        for mw in reversed(self._middlewares):
            prev_chain = chain

            async def _make_chain(middleware, next_fn):
                async def _wrapped(ctx):
                    return await middleware(ctx, next_fn)
                return _wrapped

            chain = await _make_chain(mw, prev_chain)

        return await chain(context)


# ==================== 内置中间件 ====================


async def timing_middleware(
    ctx: MiddlewareContext,
    next_fn: Callable,
) -> Any:
    """计时中间件：记录请求处理耗时"""
    start = time.monotonic()
    result = await next_fn(ctx)
    elapsed = time.monotonic() - start
    ctx.attributes["elapsed_seconds"] = elapsed
    return result


async def logging_middleware(
    ctx: MiddlewareContext,
    next_fn: Callable,
) -> Any:
    """日志中间件：记录请求开始和结束"""
    from .logger import AuditLogger
    logger = AuditLogger()
    start = time.monotonic()
    logger.log_event(
        thread_id=ctx.thread_id,
        event="system_action",
        content=f"请求开始 | provider={ctx.provider} model={ctx.model} messages={len(ctx.messages)}"
    )
    try:
        result = await next_fn(ctx)
    except Exception as exc:
        elapsed = time.monotonic() - start
        ctx.attributes["elapsed_seconds"] = elapsed
        logger.log_event(
            thread_id=ctx.thread_id,
            event="system_action",
            content=f"请求失败 | 耗时={elapsed:.2f}s error={type(exc).__name__}: {exc}"
        )
        raise

    elapsed = time.monotonic() - start
    ctx.attributes["elapsed_seconds"] = elapsed
    logger.log_event(
        thread_id=ctx.thread_id,
        event="system_action",
        content=f"请求完成 | 耗时={elapsed:.2f}s"
    )
    return result


async def rate_limit_middleware(
    ctx: MiddlewareContext,
    next_fn: Callable,
) -> Any:
    """
    速率限制中间件：防止LLM API调用过于频繁

    默认配置：每分钟最多60次请求
    """
    max_requests = ctx.attributes.get("rate_limit_rpm", 60)
    window = ctx.attributes.get("rate_limit_window", 60.0)

    # 使用滑动窗口计数
    now = time.time()
    request_times = ctx.attributes.setdefault("_rate_times", [])

    # 清理过期记录
    request_times[:] = [t for t in request_times if now - t < window]

    if len(request_times) >= max_requests:
        wait_time = window - (now - request_times[0])
        raise RuntimeError(
            f"速率限制：已达到每分钟 {max_requests} 次请求上限，"
            f"请等待 {wait_time:.1f} 秒后重试"
        )

    request_times.append(now)
    return await next_fn(ctx)
