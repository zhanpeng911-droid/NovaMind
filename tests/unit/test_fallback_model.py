"""
FallbackChatModel 链式降级测试。

覆盖关键不变量：
  - 瞬时错误（Timeout/Connection）触发降级到下一个 provider
  - 客户端错误（非瞬时）不降级，直接抛出
  - 成功后记忆活跃 provider（下次从 active 开始，不重试已失败主力）
  - bind_tools 对链内每个 model 绑定并返回新实例
"""
import asyncio
import unittest

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from novamind.core.llm.fallback_model import FallbackChatModel, _should_fallback


class _ThrowingModel:
    """模拟瞬时故障的 model（duck-type，FallbackChatModel 用 invoke 调用）。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def invoke(self, *args, **kwargs):
        raise self._exc

    async def ainvoke(self, *args, **kwargs):
        raise self._exc

    def bind_tools(self, *args, **kwargs):
        return self


class TestShouldFallback(unittest.TestCase):
    def test_timeout_error_falls_back(self):
        self.assertTrue(_should_fallback(TimeoutError("timeout")))

    def test_connection_error_falls_back(self):
        self.assertTrue(_should_fallback(ConnectionError("conn")))

    def test_value_error_does_not_fall_back(self):
        # 客户端错误（如 400/401 映射的 ValueError）不应降级
        self.assertFalse(_should_fallback(ValueError("bad request")))

    def test_key_error_does_not_fall_back(self):
        self.assertFalse(_should_fallback(KeyError("missing")))


class TestFallbackChatModel(unittest.TestCase):
    def test_falls_back_to_next_on_transient_error(self):
        good = FakeListChatModel(responses=["ok"])
        model = FallbackChatModel(
            models=[_ThrowingModel(TimeoutError("timeout")), good],
            provider_names=["bad", "good"],
        )
        result = model.invoke("hello")
        self.assertEqual(result.content, "ok")

    def test_raises_on_client_error(self):
        model = FallbackChatModel(
            models=[_ThrowingModel(ValueError("400 bad request"))],
            provider_names=["bad"],
        )
        with self.assertRaises(ValueError):
            model.invoke("hello")

    def test_remembers_active_provider(self):
        """成功后记忆 active，下次从 active 开始，不再重试已失败主力。"""
        calls = []

        class CountingModel:
            def __init__(self, name):
                self.name = name

            def invoke(self, *args, **kwargs):
                calls.append(self.name)
                from langchain_core.messages import AIMessage
                return AIMessage(content=self.name)

        m1 = CountingModel("first")
        m2 = CountingModel("second")
        model = FallbackChatModel(models=[m1, m2], provider_names=["first", "second"])

        model.invoke("a")  # first 成功，active=0
        model.invoke("b")  # 仍从 first 开始
        self.assertEqual(calls, ["first", "first"])

    def test_bind_tools_returns_new_instance(self):
        class _BindableModel:
            def __init__(self):
                self.bound_tools = None

            def invoke(self, *args, **kwargs):
                from langchain_core.messages import AIMessage
                return AIMessage(content="x")

            def bind_tools(self, tools, **kwargs):
                self.bound_tools = tools
                return self

        inner = _BindableModel()
        model = FallbackChatModel(models=[inner], provider_names=["fake"])
        bound = model.bind_tools([{"name": "t"}])
        self.assertIsInstance(bound, FallbackChatModel)
        self.assertIsNot(bound, model)

    def test_async_fallback(self):
        good = FakeListChatModel(responses=["async-ok"])
        model = FallbackChatModel(
            models=[_ThrowingModel(TimeoutError("timeout")), good],
            provider_names=["bad", "good"],
        )

        async def _run():
            return await model.ainvoke("hello")

        result = asyncio.run(_run())
        self.assertEqual(result.content, "async-ok")


if __name__ == "__main__":
    unittest.main()
