"""
状态机中间件钩子接入测试（P0）。

验证自研状态机 NovaMindAgent 能在 run 生命周期分发 before_agent / after_agent 钩子，
且默认（无中间件）时行为不变、不影响现有循环/持久化语义。
"""
import asyncio
import unittest

from langchain_core.messages import AIMessage

from novamind.core.state_machine import NovaMindAgent
from novamind.core.middlewares import (
    BaseAgentMiddleware,
    MiddlewareManager,
    MiddlewareResult,
)


def _make_agent(middlewares=None):
    agent = NovaMindAgent(middleware_manager=MiddlewareManager(middlewares))

    async def agent_node(state):
        return {"messages": [AIMessage(content="hi", id="msg_1")]}

    def route(state):
        return "__end__"

    agent.add_node("agent", agent_node)
    agent.add_conditional_edge("agent", route, {"__end__": "__end__"})
    return agent


class TestStateMachineHooks(unittest.TestCase):
    def test_hooks_invoked_in_order(self):
        calls = []

        class Recording(BaseAgentMiddleware):
            async def abefore_agent(self, ctx):
                calls.append("before_agent")

            async def aafter_agent(self, ctx):
                calls.append("after_agent")

        async def _run():
            agent = _make_agent([Recording()])
            await agent.run("hello", thread_id="test_hooks")

        asyncio.run(_run())
        self.assertEqual(calls, ["before_agent", "after_agent"])

    def test_hook_state_patch_applied(self):
        class PatchMetadata(BaseAgentMiddleware):
            async def abefore_agent(self, ctx):
                return MiddlewareResult(state_patch={"metadata": {"hooked": True}})

        async def _run():
            agent = _make_agent([PatchMetadata()])
            state = await agent.run("hello", thread_id="test_patch")
            self.assertTrue(state.metadata.get("hooked"))

        asyncio.run(_run())

    def test_default_no_middleware_unchanged(self):
        """无中间件时行为与改造前一致，正常跑完一轮。"""
        async def _run():
            agent = _make_agent([])
            state = await agent.run("hello", thread_id="test_default")
            self.assertTrue(any(m.content == "hi" for m in state.messages))

        asyncio.run(_run())

    def test_middleware_receives_state_and_thread(self):
        captured = {}

        class Inspect(BaseAgentMiddleware):
            async def abefore_agent(self, ctx):
                captured["thread_id"] = ctx.thread_id
                captured["has_state"] = ctx.state is not None

        async def _run():
            agent = _make_agent([Inspect()])
            await agent.run("hello", thread_id="inspect_me")

        asyncio.run(_run())
        self.assertEqual(captured["thread_id"], "inspect_me")
        self.assertTrue(captured["has_state"])


if __name__ == "__main__":
    unittest.main()
