"""
agent.py 接线端到端测试。

验证 create_agent_app 接入横切中间件钩子（before_model/after_model/wrap_tool_call）后，
走真实 agent_node/tool_executor 全链路时钩子确实被调用。默认（无中间件）行为不变。
"""
import asyncio
import unittest
import uuid
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from novamind.core.middlewares import BaseAgentMiddleware, MiddlewareResult


class FakeLLM:
    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self._i = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        if self._i < len(self._responses):
            resp = self._responses[self._i]
            self._i += 1
            return resp
        return AIMessage(content="done")


class FakeAudit:
    def __init__(self):
        self.events = []

    def log_event(self, thread_id, event, **kwargs):
        self.events.append((event, kwargs))


class _Recording(BaseAgentMiddleware):
    def __init__(self):
        self.calls = []

    async def abefore_model(self, ctx):
        self.calls.append("before_model")
        return MiddlewareResult(messages_patch=[HumanMessage(content="[memory]", name="memory_recall")])

    async def aafter_model(self, ctx):
        self.calls.append("after_model")

    async def awrap_tool_call(self, ctx):
        self.calls.append("wrap_tool_call")


def _build(middlewares=None, llm_responses=None):
    fake_llm = FakeLLM(llm_responses)
    with patch("novamind.core.agent.get_provider", return_value=fake_llm), \
            patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
            patch("novamind.core.agent.load_mcp_tools", return_value=[]):
        from novamind.core.agent import create_agent_app
        return create_agent_app(audit_logger=FakeAudit(), tools=[], middlewares=middlewares)


class TestAgentWiring(unittest.TestCase):
    def test_before_model_hook_invoked(self):
        rec = _Recording()
        agent = _build(middlewares=[rec])

        async def _run():
            await agent.run("hello", thread_id="wiring_" + uuid.uuid4().hex[:6])

        asyncio.run(_run())
        self.assertIn("before_model", rec.calls)
        self.assertIn("after_model", rec.calls)

    def test_wrap_tool_call_hook_invoked(self):
        rec = _Recording()
        agent = _build(middlewares=[rec], llm_responses=[
            AIMessage(content="", tool_calls=[{"name": "get_current_time", "args": {}, "id": "tc1"}]),
            AIMessage(content="done"),
        ])

        async def _run():
            await agent.run("what time", thread_id="wiring_" + uuid.uuid4().hex[:6])

        asyncio.run(_run())
        self.assertIn("wrap_tool_call", rec.calls)

    def test_no_middleware_unchanged(self):
        """无中间件时正常跑通（向后兼容）。"""
        agent = _build(middlewares=None)

        async def _run():
            state = await agent.run("hi", thread_id="wiring_" + uuid.uuid4().hex[:6])
            self.assertTrue(any(m.content == "done" for m in state.messages if isinstance(m, AIMessage)))

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
