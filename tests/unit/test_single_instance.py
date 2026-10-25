"""A0 单实例锁测试。"""

from __future__ import annotations

import unittest
import uuid

from novamind.desktop.single_instance import SingleInstance


class TestSingleInstance(unittest.TestCase):
    def test_second_instance_rejected_then_released(self):
        name = "Local\\NovaMind.Test." + uuid.uuid4().hex
        first = SingleInstance(name)
        second = SingleInstance(name)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_context_manager(self):
        name = "Local\\NovaMind.Test." + uuid.uuid4().hex
        with SingleInstance(name) as lock:
            self.assertTrue(lock.acquired)


if __name__ == "__main__":
    unittest.main()
