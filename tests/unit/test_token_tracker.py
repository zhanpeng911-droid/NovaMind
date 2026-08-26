"""
NovaMind Token追踪器测试

测试Token统计和成本估算功能。
"""
import unittest
from novamind.core.token_tracker import TokenTracker


class TestTokenTracker(unittest.TestCase):
    """测试 TokenTracker 核心功能"""

    def setUp(self):
        self.tracker = TokenTracker(cost_warning_usd=10.0)

    def test_record_basic(self):
        """测试基本记录功能"""
        usage = self.tracker.record(
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            thread_id="test_basic",
        )
        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual(usage.completion_tokens, 50)
        self.assertEqual(usage.total_tokens, 150)
        self.assertGreater(usage.estimated_cost_usd, 0)

    def test_session_stats(self):
        """测试会话聚合统计"""
        self.tracker.record("gpt-4o", 100, 50, "test_stats")
        self.tracker.record("gpt-4o-mini", 200, 100, "test_stats")

        stats = self.tracker.get_session_stats("test_stats")
        self.assertEqual(stats.call_count, 2)
        self.assertEqual(stats.total_prompt_tokens, 300)
        self.assertEqual(stats.total_completion_tokens, 150)
        self.assertEqual(stats.total_tokens, 450)
        self.assertGreater(stats.total_cost_usd, 0)

    def test_cost_estimation_accuracy(self):
        """测试成本估算的准确性"""
        # GPT-4o 定价: 输入 $0.0025/1K, 输出 $0.01/1K
        usage = self.tracker.record("gpt-4o", 1000, 1000, "test_cost")
        expected_cost = (1000 * 0.0025 + 1000 * 0.01) / 1000
        self.assertAlmostEqual(usage.estimated_cost_usd, expected_cost, places=6)

    def test_unknown_model_uses_default_pricing(self):
        """测试未知模型使用默认定价"""
        usage = self.tracker.record("unknown_model", 1000, 1000, "test_default")
        self.assertGreater(usage.estimated_cost_usd, 0)

    def test_thread_isolation(self):
        """测试不同线程的数据隔离"""
        self.tracker.record("gpt-4o", 100, 50, "thread_A")
        self.tracker.record("gpt-4o", 200, 100, "thread_B")

        stats_a = self.tracker.get_session_stats("thread_A")
        stats_b = self.tracker.get_session_stats("thread_B")

        self.assertEqual(stats_a.call_count, 1)
        self.assertEqual(stats_b.call_count, 1)

    def test_reset(self):
        """测试重置功能"""
        self.tracker.record("gpt-4o", 100, 50, "test_reset")
        self.tracker.reset("test_reset")

        stats = self.tracker.get_session_stats("test_reset")
        self.assertEqual(stats.call_count, 0)

    def test_format_stats(self):
        """测试统计信息格式化输出"""
        self.tracker.record("gpt-4o", 100, 50, "test_format")
        output = self.tracker.format_stats("test_format")
        self.assertIn("test_format", output)
        self.assertIn("Token", output)

    def test_get_all_thread_ids(self):
        """测试获取所有线程ID"""
        self.tracker.record("gpt-4o", 100, 50, "thread_1")
        self.tracker.record("gpt-4o", 100, 50, "thread_2")

        ids = self.tracker.get_all_thread_ids()
        self.assertIn("thread_1", ids)
        self.assertIn("thread_2", ids)


if __name__ == "__main__":
    unittest.main()
