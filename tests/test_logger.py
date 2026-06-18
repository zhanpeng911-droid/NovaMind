"""
NovaMind audit logger safety tests.
"""
import unittest

from novamind.core.logger import _sanitize_for_log


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


if __name__ == "__main__":
    unittest.main()
