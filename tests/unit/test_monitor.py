"""
NovaMind Monitor 目标解析测试

测试内容：
  - 会话发现（list_sessions）
  - 目标解析（resolve_log_target）
  - thread_id 到文件名的转换
  - 边界情况处理
"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch


class TestSafeIdFromThreadId(unittest.TestCase):
    """测试 thread_id 到安全文件名的转换"""

    def test_normal_thread_id(self):
        from entry.monitor import _safe_id_from_thread_id
        self.assertEqual(_safe_id_from_thread_id("my_session"), "my_session")

    def test_thread_id_with_special_chars(self):
        from entry.monitor import _safe_id_from_thread_id
        result = _safe_id_from_thread_id("session/123:test")
        self.assertNotIn("/", result)
        self.assertNotIn(":", result)

    def test_empty_thread_id(self):
        from entry.monitor import _safe_id_from_thread_id
        self.assertEqual(_safe_id_from_thread_id(""), "default")

    def test_thread_id_preserves_alphanumeric_and_hyphens(self):
        from entry.monitor import _safe_id_from_thread_id
        self.assertEqual(_safe_id_from_thread_id("abc-123_def"), "abc-123_def")


class TestListSessions(unittest.TestCase):
    """测试会话发现"""

    def test_lists_jsonl_files(self):
        from entry.monitor import list_sessions
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建几个 .jsonl 文件
            for name in ["session_a.jsonl", "session_b.jsonl", "not_log.txt"]:
                fpath = os.path.join(tmpdir, name)
                with open(fpath, "w") as f:
                    f.write('{"event": "test"}\n')
                # 给不同文件不同的修改时间
                if name == "session_b.jsonl":
                    time.sleep(0.05)

            with patch("entry.monitor.LOG_DIR", tmpdir):
                sessions = list_sessions()

            self.assertEqual(len(sessions), 2)
            thread_ids = {s["thread_id"] for s in sessions}
            self.assertIn("session_a", thread_ids)
            self.assertIn("session_b", thread_ids)

    def test_returns_empty_when_no_logs(self):
        from entry.monitor import list_sessions
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("entry.monitor.LOG_DIR", tmpdir):
                sessions = list_sessions()
            self.assertEqual(sessions, [])

    def test_returns_empty_when_log_dir_missing(self):
        from entry.monitor import list_sessions
        with patch("entry.monitor.LOG_DIR", "/nonexistent/path/xyz"):
            sessions = list_sessions()
        self.assertEqual(sessions, [])

    def test_sorted_by_most_recent(self):
        from entry.monitor import list_sessions
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建两个文件，old 先创建
            old_path = os.path.join(tmpdir, "old.jsonl")
            new_path = os.path.join(tmpdir, "new.jsonl")
            with open(old_path, "w") as f:
                f.write("old\n")
            time.sleep(0.05)
            with open(new_path, "w") as f:
                f.write("new\n")

            with patch("entry.monitor.LOG_DIR", tmpdir):
                sessions = list_sessions()

            self.assertEqual(len(sessions), 2)
            self.assertEqual(sessions[0]["thread_id"], "new")
            self.assertEqual(sessions[1]["thread_id"], "old")


class TestResolveLogTarget(unittest.TestCase):
    """测试监控目标解析"""

    def test_explicit_thread_id_returns_correct_path(self):
        from entry.monitor import resolve_log_target
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "my_session.jsonl")
            with open(log_path, "w") as f:
                f.write("test\n")

            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target("my_session")

            self.assertEqual(result, log_path)

    def test_explicit_thread_id_not_found_returns_none(self):
        from entry.monitor import resolve_log_target
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target("nonexistent")
            self.assertIsNone(result)

    def test_no_thread_id_picks_most_recent(self):
        from entry.monitor import resolve_log_target
        with tempfile.TemporaryDirectory() as tmpdir:
            old_path = os.path.join(tmpdir, "old.jsonl")
            new_path = os.path.join(tmpdir, "new.jsonl")
            with open(old_path, "w") as f:
                f.write("old\n")
            time.sleep(0.05)
            with open(new_path, "w") as f:
                f.write("new\n")

            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target(None)

            self.assertEqual(result, new_path)

    def test_no_thread_id_and_no_logs_returns_none(self):
        from entry.monitor import resolve_log_target
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target(None)
            self.assertIsNone(result)

    def test_thread_id_with_special_chars_resolves(self):
        """验证带特殊字符的 thread_id 经过 safe_id 转换后能找到对应文件"""
        from entry.monitor import resolve_log_target, _safe_id_from_thread_id
        with tempfile.TemporaryDirectory() as tmpdir:
            thread_id = "my/session:123"
            safe_id = _safe_id_from_thread_id(thread_id)
            log_path = os.path.join(tmpdir, f"{safe_id}.jsonl")
            with open(log_path, "w") as f:
                f.write("test\n")

            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target(thread_id)

            self.assertEqual(result, log_path)


if __name__ == "__main__":
    unittest.main()
