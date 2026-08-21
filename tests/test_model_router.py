"""
ModelRouter + route_chain_for 路由测试。

覆盖关键不变量：
  - 兜底 = is_default provider 恒在降级链尾（已敲定决策，不硬编码 deepseek）
  - 角色路由链按 MODEL_ROUTES 顺序筛可用 provider
  - 无可用 provider 抛 ProviderConfigError
  - build_single 用 fake provider 可无 key 构造
"""
import unittest

from novamind.core.llm.provider_config import (
    ProviderConfig,
    ProviderConfigError,
    route_chain_for,
)
from novamind.core.llm.model_router import ModelRouter


def _cfg(name, *, default=False, priority=0):
    return ProviderConfig(
        provider=name,
        model=f"{name}-model",
        api_key=f"key-{name}" if name != "fake" else "",
        base_url=None,
        priority=priority,
        default=default,
        enabled=True,
        window=128_000,
    )


class TestRouteChainFor(unittest.TestCase):
    def test_default_provider_always_last(self):
        """is_default provider 恒在链尾，即使路由顺序里它排首位。"""
        providers = [
            _cfg("openai", default=True, priority=10),
            _cfg("qwen", priority=20),
            _cfg("deepseek", priority=70),
        ]
        chain = route_chain_for("researcher", providers)
        names = [p.provider for p in chain]
        self.assertEqual(names[-1], "openai")
        self.assertEqual(set(names), {"openai", "qwen", "deepseek"})

    def test_default_not_in_route_still_appended(self):
        """默认 provider 不在角色路由中时，追加到链尾。"""
        providers = [
            _cfg("openai", default=True, priority=10),
            _cfg("zhipu", priority=40),
            _cfg("hunyuan", priority=30),
        ]
        # reflection 路由只偏好 deepseek，但不可用，链应由默认 provider 兜底
        chain = route_chain_for("reflection", providers)
        names = [p.provider for p in chain]
        self.assertEqual(names[-1], "openai")

    def test_no_default_falls_back_to_route_order(self):
        """无 is_default 时按路由顺序 + priority。"""
        providers = [
            _cfg("qwen", priority=20),
            _cfg("deepseek", priority=70),
        ]
        chain = route_chain_for("researcher", providers)
        names = [p.provider for p in chain]
        self.assertEqual(names, ["qwen", "deepseek"])

    def test_empty_chain_raises(self):
        with self.assertRaises(ProviderConfigError):
            route_chain_for("researcher", [])

    def test_unknown_role_uses_default_only(self):
        providers = [
            _cfg("openai", default=True, priority=10),
            _cfg("qwen", priority=20),
        ]
        chain = route_chain_for("unknown_role", providers)
        names = [p.provider for p in chain]
        self.assertEqual(names[-1], "openai")


class TestModelRouter(unittest.TestCase):
    def test_chain_names_orders_default_last(self):
        providers = [
            _cfg("openai", default=True, priority=10),
            _cfg("qwen", priority=20),
            _cfg("zhipu", priority=40),
        ]
        router = ModelRouter(providers=providers)
        names = router.chain_names("researcher")
        self.assertEqual(names[-1], "openai")

    def test_build_single_fake_no_key(self):
        """fake provider 无需 api_key 即可构造。"""
        providers = [_cfg("fake")]
        router = ModelRouter(providers=providers)
        model = router.build_single("fake")
        self.assertIsNotNone(model)


if __name__ == "__main__":
    unittest.main()
