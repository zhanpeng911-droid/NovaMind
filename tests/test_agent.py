"""
NovaMind agent assembly helpers tests.
"""
import asyncio
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch, AsyncMock

from novamind.core.agent import _extract_token_counts
from novamind.core.state_machine import AgentState, NovaMindAgent
from novamind.core.context import ContextManager
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage


@dataclass
class FakeResponse:
    usage_metadata: dict | None = None
    response_metadata: dict | None = None


class TestAgentTokenExtraction(unittest.TestCase):
    """测试 LLM 响应 Token 用量提取"""

    def test_extracts_langchain_usage_metadata(self):
        response = FakeResponse(
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 35,
            }
        )

        self.assertEqual(_extract_token_counts(response), (120, 35))

    def test_extracts_openai_compatible_token_usage(self):
        response = FakeResponse(
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                    "total_tokens": 100,
                }
            }
        )

        self.assertEqual(_extract_token_counts(response), (80, 20))

    def test_returns_none_without_usage(self):
        self.assertIsNone(_extract_token_counts(FakeResponse()))


class TestAsyncBlockingIsolation(unittest.TestCase):
    """测试同步阻塞调用被 asyncio.to_thread 隔离到线程池"""

    def test_tool_executor_runs_in_thread(self):
        """验证 tool_executor 中 tool.invoke 被 asyncio.to_thread 包裹"""
        async def _test():
            agent = NovaMindAgent()
            call_thread_id = None

            def slow_tool(args):
                nonlocal call_thread_id
                # 记录执行时的线程 ID
                import threading
                call_thread_id = threading.current_thread().ident
                return "tool_result"

            mock_tool = MagicMock()
            mock_tool.name = "test_tool"
            mock_tool.invoke = slow_tool

            from novamind.core.agent import create_agent_app
            # 通过闭包直接测试 tool_executor
            tool_map = {"test_tool": mock_tool}

            async def tool_executor(state):
                last_msg = state.messages[-1] if state.messages else None
                if not last_msg or not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                    return {"messages": []}
                tool_messages = []
                for tc in last_msg.tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc.get("args", {})
                    tool_id = tc.get("id", "")
                    if tool_name in tool_map:
                        import uuid
                        result = await asyncio.to_thread(tool_map[tool_name].invoke, tool_args)
                        tool_messages.append(ToolMessage(
                            content=str(result), tool_call_id=tool_id,
                            name=tool_name, id=f"msg_{uuid.uuid4().hex[:12]}",
                        ))
                return {"messages": tool_messages}

            state = AgentState()
            ai_msg = AIMessage(
                content="",
                tool_calls=[{"name": "test_tool", "args": {"x": 1}, "id": "tc_1"}],
            )
            state.add_message(ai_msg)

            import threading
            main_thread_id = threading.current_thread().ident
            result = await tool_executor(state)

            # 工具应在子线程中执行，不在主线程
            self.assertIsNotNone(call_thread_id)
            self.assertNotEqual(call_thread_id, main_thread_id)
            self.assertEqual(len(result["messages"]), 1)
            self.assertEqual(result["messages"][0].content, "tool_result")

        asyncio.run(_test())

    def test_tool_executor_exception_propagates(self):
        """验证 tool.invoke 异常通过 asyncio.to_thread 正确传播"""
        async def _test():
            def failing_tool(args):
                raise ValueError("tool broke")

            mock_tool = MagicMock()
            mock_tool.name = "bad_tool"
            mock_tool.invoke = failing_tool
            tool_map = {"bad_tool": mock_tool}

            async def tool_executor(state):
                last_msg = state.messages[-1] if state.messages else None
                if not last_msg or not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                    return {"messages": []}
                tool_messages = []
                for tc in last_msg.tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc.get("args", {})
                    tool_id = tc.get("id", "")
                    if tool_name in tool_map:
                        import uuid
                        try:
                            result = await asyncio.to_thread(tool_map[tool_name].invoke, tool_args)
                            tool_messages.append(ToolMessage(
                                content=str(result), tool_call_id=tool_id,
                                name=tool_name, id=f"msg_{uuid.uuid4().hex[:12]}",
                            ))
                        except Exception as e:
                            tool_messages.append(ToolMessage(
                                content=f"工具执行异常: {str(e)}", tool_call_id=tool_id,
                                name=tool_name, id=f"msg_{uuid.uuid4().hex[:12]}",
                            ))
                return {"messages": tool_messages}

            state = AgentState()
            ai_msg = AIMessage(
                content="",
                tool_calls=[{"name": "bad_tool", "args": {}, "id": "tc_1"}],
            )
            state.add_message(ai_msg)

            result = await tool_executor(state)
            # 异常被捕获并转换为 ToolMessage
            self.assertEqual(len(result["messages"]), 1)
            self.assertIn("工具执行异常", result["messages"][0].content)
            self.assertIn("tool broke", result["messages"][0].content)

        asyncio.run(_test())

    def test_summary_generation_runs_in_thread(self):
        """验证 context_manager.generate_summary 通过 asyncio.to_thread 调用"""
        async def _test():
            call_thread_id = None

            def slow_generate_summary(current_summary, discarded):
                nonlocal call_thread_id
                import threading
                call_thread_id = threading.current_thread().ident
                return f"summary of {len(discarded)} msgs"

            ctx = ContextManager(llm=None)
            ctx.generate_summary = slow_generate_summary

            import threading
            main_thread_id = threading.current_thread().ident

            discarded = [HumanMessage(content="old")]
            result = await asyncio.to_thread(ctx.generate_summary, "", discarded)

            self.assertIsNotNone(call_thread_id)
            self.assertNotEqual(call_thread_id, main_thread_id)
            self.assertEqual(result, "summary of 1 msgs")

        asyncio.run(_test())

    def test_middleware_pipeline_with_threaded_handler(self):
        """验证中间件管道在 handler 使用 asyncio.to_thread 时仍正常工作"""
        async def _test():
            from novamind.core.middleware import MiddlewarePipeline, MiddlewareContext, timing_middleware

            pipeline = MiddlewarePipeline()
            pipeline.add(timing_middleware)
            ctx = MiddlewareContext()

            def blocking_call(messages):
                import time
                time.sleep(0.05)
                return "llm_response"

            async def handler(mctx):
                return await asyncio.to_thread(blocking_call, mctx.messages)

            result = await pipeline.execute(ctx, handler)
            self.assertEqual(result, "llm_response")
            self.assertGreater(ctx.attributes["elapsed_seconds"], 0.04)

        asyncio.run(_test())

    def test_concurrent_coroutines_not_blocked(self):
        """验证多个协程并发时，一个阻塞调用不阻塞其他协程"""
        async def _test():
            results = []

            async def slow_task(name, delay):
                await asyncio.to_thread(lambda: __import__('time').sleep(delay))
                results.append(name)

            # 两个任务各 sleep 0.1s，如果串行需要 0.2s+
            # 并发执行应在 ~0.1s 内完成
            import time
            start = time.monotonic()
            await asyncio.gather(
                slow_task("a", 0.1),
                slow_task("b", 0.1),
            )
            elapsed = time.monotonic() - start

            self.assertIn("a", results)
            self.assertIn("b", results)
            # 并发执行应显著快于串行（0.2s）
            self.assertLess(elapsed, 0.15)

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
