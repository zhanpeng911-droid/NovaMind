"""
NovaMind 中间件管道测试

测试中间件的执行顺序、上下文传递和错误处理。
"""
import asyncio
import unittest
from novamind.core.middleware import (
    MiddlewarePipeline, MiddlewareContext,
    timing_middleware, logging_middleware, rate_limit_middleware,
)


class TestMiddlewarePipeline(unittest.TestCase):
    """测试 MiddlewarePipeline 核心功能"""

    def test_empty_pipeline(self):
        """测试空管道直接执行handler"""
        async def _test():
            pipeline = MiddlewarePipeline()
            ctx = MiddlewareContext()

            async def handler(mctx):
                return "result"

            result = await pipeline.execute(ctx, handler)
            self.assertEqual(result, "result")

        asyncio.run(_test())

    def test_single_middleware(self):
        """测试单个中间件"""
        async def _test():
            pipeline = MiddlewarePipeline()
            ctx = MiddlewareContext()

            async def my_middleware(ctx, next_fn):
                ctx.attributes["before"] = True
                result = await next_fn(ctx)
                ctx.attributes["after"] = True
                return result

            pipeline.add(my_middleware)

            async def handler(mctx):
                return "done"

            result = await pipeline.execute(ctx, handler)
            self.assertEqual(result, "done")
            self.assertTrue(ctx.attributes["before"])
            self.assertTrue(ctx.attributes["after"])

        asyncio.run(_test())

    def test_middleware_order(self):
        """测试中间件执行顺序（洋葱模型）"""
        async def _test():
            pipeline = MiddlewarePipeline()
            ctx = MiddlewareContext()
            order = []

            async def outer(ctx, next_fn):
                order.append("outer_before")
                result = await next_fn(ctx)
                order.append("outer_after")
                return result

            async def inner(ctx, next_fn):
                order.append("inner_before")
                result = await next_fn(ctx)
                order.append("inner_after")
                return result

            pipeline.add(outer)
            pipeline.add(inner)

            async def handler(mctx):
                order.append("handler")
                return "done"

            await pipeline.execute(ctx, handler)
            self.assertEqual(order, [
                "outer_before", "inner_before", "handler",
                "inner_after", "outer_after",
            ])

        asyncio.run(_test())

    def test_timing_middleware(self):
        """测试计时中间件"""
        async def _test():
            pipeline = MiddlewarePipeline()
            pipeline.add(timing_middleware)
            ctx = MiddlewareContext()

            async def slow_handler(mctx):
                await asyncio.sleep(0.05)
                return "done"

            await pipeline.execute(ctx, slow_handler)
            self.assertGreater(ctx.attributes["elapsed_seconds"], 0.04)

        asyncio.run(_test())

    def test_logging_middleware_records_elapsed(self):
        """测试日志中间件独立记录真实耗时"""
        async def _test():
            pipeline = MiddlewarePipeline()
            pipeline.add(logging_middleware)
            ctx = MiddlewareContext(thread_id="test_logging_elapsed")

            async def slow_handler(mctx):
                await asyncio.sleep(0.05)
                return "done"

            await pipeline.execute(ctx, slow_handler)
            self.assertGreater(ctx.attributes["elapsed_seconds"], 0.04)

        asyncio.run(_test())

    def test_rate_limit_middleware(self):
        """测试速率限制中间件"""
        async def _test():
            pipeline = MiddlewarePipeline()
            pipeline.add(rate_limit_middleware)
            ctx = MiddlewareContext()
            ctx.attributes["rate_limit_rpm"] = 3
            ctx.attributes["rate_limit_window"] = 1.0

            async def handler(mctx):
                return "ok"

            # 前3次应该成功
            for _ in range(3):
                await pipeline.execute(ctx, handler)

            # 第4次应该触发速率限制
            with self.assertRaises(RuntimeError) as cm:
                await pipeline.execute(ctx, handler)
            self.assertIn("速率限制", str(cm.exception))

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
