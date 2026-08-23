"""
横切中间件协议 + MiddlewareManager 测试。

覆盖：
  - 空 manager 分发返回空 result
  - 单中间件钩子被调用（同步 + async 优先）
  - 多中间件按注册顺序调用
  - messages_patch 拼接合并、state_patch 浅合并、override 后者覆盖
"""
import asyncio
import unittest

from novamind.core.middlewares.protocol import (
    BaseAgentMiddleware,
    MiddlewareContext,
    MiddlewareResult,
)
from novamind.core.middlewares.manager import MiddlewareManager


class _RecordingMiddleware(BaseAgentMiddleware):
    def __init__(self):
        self.calls = []

    async def abefore_model(self, ctx):
        self.calls.append("before_model")
        return MiddlewareResult(messages_patch=[{"from": "mw1"}])


class _StatePatchMiddleware(BaseAgentMiddleware):
    async def abefore_model(self, ctx):
        return MiddlewareResult(state_patch={"k": 1})

    async def abefore_model_second(self, ctx):
        return MiddlewareResult(state_patch={"k": 2})


class TestMiddlewareManager(unittest.TestCase):
    def test_empty_manager_returns_empty_result(self):
        async def _run():
            mgr = MiddlewareManager()
            result = await mgr.dispatch("before_model", MiddlewareContext())
            self.assertTrue(result.is_empty())

        asyncio.run(_run())

    def test_hook_is_invoked(self):
        async def _run():
            mw = _RecordingMiddleware()
            mgr = MiddlewareManager([mw])
            result = await mgr.dispatch("before_model", MiddlewareContext())
            self.assertEqual(mw.calls, ["before_model"])
            self.assertEqual(result.messages_patch, [{"from": "mw1"}])

        asyncio.run(_run())

    def test_dispatch_order(self):
        async def _run():
            order = []

            class A(BaseAgentMiddleware):
                async def abefore_model(self, ctx):
                    order.append("a")

            class B(BaseAgentMiddleware):
                async def abefore_model(self, ctx):
                    order.append("b")

            mgr = MiddlewareManager([A(), B()])
            await mgr.dispatch("before_model", MiddlewareContext())
            self.assertEqual(order, ["a", "b"])

        asyncio.run(_run())

    def test_messages_patch_concatenates(self):
        async def _run():
            class A(BaseAgentMiddleware):
                async def abefore_model(self, ctx):
                    return MiddlewareResult(messages_patch=["m1"])

            class B(BaseAgentMiddleware):
                async def abefore_model(self, ctx):
                    return MiddlewareResult(messages_patch=["m2", "m3"])

            mgr = MiddlewareManager([A(), B()])
            result = await mgr.dispatch("before_model", MiddlewareContext())
            self.assertEqual(result.messages_patch, ["m1", "m2", "m3"])

        asyncio.run(_run())

    def test_state_patch_shallow_merges_later_wins(self):
        async def _run():
            class A(BaseAgentMiddleware):
                async def abefore_model(self, ctx):
                    return MiddlewareResult(state_patch={"a": 1, "shared": "a"})

            class B(BaseAgentMiddleware):
                async def abefore_model(self, ctx):
                    return MiddlewareResult(state_patch={"b": 2, "shared": "b"})

            mgr = MiddlewareManager([A(), B()])
            result = await mgr.dispatch("before_model", MiddlewareContext())
            self.assertEqual(result.state_patch, {"a": 1, "b": 2, "shared": "b"})

        asyncio.run(_run())

    def test_override_later_wins(self):
        async def _run():
            class A(BaseAgentMiddleware):
                async def awrap_tool_call(self, ctx):
                    return MiddlewareResult(override="from_a")

            class B(BaseAgentMiddleware):
                async def awrap_tool_call(self, ctx):
                    return MiddlewareResult(override="from_b")

            mgr = MiddlewareManager([A(), B()])
            result = await mgr.dispatch("wrap_tool_call", MiddlewareContext())
            self.assertEqual(result.override, "from_b")

        asyncio.run(_run())

    def test_async_preferred_over_sync(self):
        async def _run():
            class SyncOnly(BaseAgentMiddleware):
                def before_model(self, ctx):
                    return MiddlewareResult(state_patch={"sync": True})

            mgr = MiddlewareManager([SyncOnly()])
            result = await mgr.dispatch("before_model", MiddlewareContext())
            self.assertEqual(result.state_patch, {"sync": True})

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
