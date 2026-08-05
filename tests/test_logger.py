"""
NovaMind audit logger safety tests.
"""
import os
import tempfile
import unittest

from novamind.core.logger import AuditLogger, _sanitize_for_log


class TestLoggerSanitization(unittest.TestCase):
    """测试审计日志脱敏"""

    def test_redacts_sensitive_keys_recursively(self):
        data = {
            "args": {
                "api_key": "sk-secret-value",
                "nested": {"password": "p@ssw0rd"},
            }
        }

        sanitized = _sanitize_for_log(data)
        self.assertEqual(sanitized["args"]["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["args"]["nested"]["password"], "[REDACTED]")

    def test_redacts_secret_like_values_and_truncates(self):
        value = "token sk-abcdefghijklmnopqrstuvwxyz0123456789 " + ("x" * 3000)
        sanitized = _sanitize_for_log(value)

        self.assertIn("[REDACTED]", sanitized)
        self.assertLessEqual(len(sanitized), 2014)


class TestLoggerQueue(unittest.TestCase):
    """测试审计日志队列背压与 shutdown 安全"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = self._tmpdir.name
        # 用一个小队列测试背压
        os.environ["NOVAMIND_LOG_QUEUE_SIZE"] = "3"
        # 重置单例以便用新配置初始化
        AuditLogger._instance = None
        self.logger = AuditLogger(log_dir=self.log_dir)

    def tearDown(self):
        self.logger.shutdown()
        AuditLogger._instance = None
        os.environ.pop("NOVAMIND_LOG_QUEUE_SIZE", None)
        self._tmpdir.cleanup()

    def test_queue_is_bounded(self):
        """队列应有 maxsize 限制"""
        self.assertEqual(self.logger._buffer.maxlen, 3)

    def test_backpressure_drops_oldest(self):
        """队列满时丢弃低优先级事件，新事件仍能入队"""
        # 填满队列（llm_input 是 low 优先级，会被驱逐）
        for i in range(3):
            self.logger.log_event("test_thread", "llm_input", index=i)

        # 队列已满，再多发几条应触发驱逐
        for i in range(3, 6):
            self.logger.log_event("test_thread", "llm_input", index=i)

        # 丢弃计数应 > 0
        self.assertGreater(self.logger._dropped_count, 0)

    def test_critical_events_never_dropped(self):
        """关键事件（policy_violation）不应被驱逐"""
        # 填满队列：3 个 low 优先级
        for i in range(3):
            self.logger.log_event("test_thread", "llm_input", index=i)

        # 队列已满，发一个关键事件
        self.logger.log_event("test_thread", "policy_violation", reason="test")

        # 等待队列消费
        import time as _time
        _time.sleep(0.3)

        # 关键事件应该被写入文件（而不是被丢弃）
        log_files = [f for f in os.listdir(self.log_dir) if f.endswith(".jsonl")]
        self.assertTrue(log_files)
        with open(os.path.join(self.log_dir, log_files[0]), "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("policy_violation", content)

    def test_low_priority_dropped_before_normal(self):
        """low 优先级事件优先于 normal 被驱逐"""
        # 填满队列：2 个 low + 1 个 normal
        self.logger.log_event("test_thread", "llm_input", idx=0)        # low
        self.logger.log_event("test_thread", "llm_input", idx=1)        # low
        self.logger.log_event("test_thread", "ai_message", content="x") # normal

        # 队列已满，再发一个 normal 事件，应驱逐一个 low
        self.logger.log_event("test_thread", "ai_message", content="y")

        # 驱逐的应该是 low 优先级事件
        self.assertGreater(self.logger._dropped_count, 0)
        dropped_low = self.logger._dropped_by_event.get("llm_input", 0)
        dropped_normal = self.logger._dropped_by_event.get("ai_message", 0)
        self.assertGreater(dropped_low, 0)
        self.assertEqual(dropped_normal, 0)

    def test_double_shutdown_no_deadlock(self):
        """连续调用 shutdown 不应死锁"""
        self.logger.shutdown()
        # 第二次调用应立即返回，不阻塞
        self.logger.shutdown()
        self.assertTrue(self.logger._stopped)


if __name__ == "__main__":
    unittest.main()
