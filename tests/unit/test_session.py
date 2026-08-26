"""
NovaMind 会话隔离测试

测试内容：
  - thread_id 生成格式与唯一性
  - 默认新会话与指定 thread_id 的行为差异
  - 不同 thread_id 的日志/状态隔离
"""
import re
import time
import unittest


class TestGenerateThreadId(unittest.TestCase):
    """测试 thread_id 生成"""

    def test_format_matches_pattern(self):
        from entry.main import generate_thread_id
        tid = generate_thread_id()
        # session_YYYYMMDD_HHMMSS_XXXXXXXX
        self.assertTrue(
            re.match(r"^session_\d{8}_\d{6}_[0-9a-f]{8}$", tid),
            f"thread_id 格式不符合预期: {tid}",
        )

    def test_unique_across_calls(self):
        from entry.main import generate_thread_id
        ids = {generate_thread_id() for _ in range(50)}
        # 50 次调用应产生 50 个唯一 ID（极低碰撞概率）
        self.assertEqual(len(ids), 50)

    def test_contains_date_prefix(self):
        from entry.main import generate_thread_id
        tid = generate_thread_id()
        date_part = tid.split("_")[1]  # YYYYMMDD
        self.assertEqual(len(date_part), 8)
        self.assertTrue(date_part.isdigit())


class TestSessionIsolation(unittest.TestCase):
    """测试不同 thread_id 的状态/日志隔离"""

    def test_different_thread_ids_produce_different_log_files(self):
        """验证不同 thread_id 映射到不同日志文件"""
        from entry.monitor import _safe_id_from_thread_id
        tid_a = "session_20260101_120000_aaaa1111"
        tid_b = "session_20260101_120000_bbbb2222"
        safe_a = _safe_id_from_thread_id(tid_a)
        safe_b = _safe_id_from_thread_id(tid_b)
        self.assertNotEqual(safe_a, safe_b)

    def test_same_thread_id_maps_to_same_log_file(self):
        """验证相同 thread_id 始终映射到同一日志文件"""
        from entry.monitor import _safe_id_from_thread_id
        tid = "session_20260101_120000_aaaa1111"
        self.assertEqual(_safe_id_from_thread_id(tid), _safe_id_from_thread_id(tid))

    def test_main_accepts_thread_id_parameter(self):
        """验证 main() 和 async_main() 接受 thread_id 参数"""
        import inspect
        from entry.main import main, async_main
        # main() 应有 thread_id 参数
        sig_main = inspect.signature(main)
        self.assertIn("thread_id", sig_main.parameters)
        # async_main() 应有 thread_id 参数
        sig_async = inspect.signature(async_main)
        self.assertIn("thread_id", sig_async.parameters)

    def test_cli_run_accepts_thread_id_option(self):
        """验证 cli.py run 命令支持 --thread-id 选项"""
        from typer.testing import CliRunner
        from entry.cli import app
        runner = CliRunner()
        # --help 应该显示 thread-id 选项
        result = runner.invoke(app, ["run", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--thread-id", result.output)


class TestMonitorTargetAlignment(unittest.TestCase):
    """测试 monitor 与 run 的默认目标对齐"""

    def test_resolve_picks_newest_log(self):
        """验证 monitor 默认选择最新日志文件"""
        import os
        import tempfile
        from unittest.mock import patch
        from entry.monitor import resolve_log_target

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建两个日志文件
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

    def test_resolve_explicit_thread_id(self):
        """验证 monitor 可以定位指定 thread_id 的日志"""
        import os
        import tempfile
        from unittest.mock import patch
        from entry.monitor import resolve_log_target

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "my_session.jsonl")
            with open(log_path, "w") as f:
                f.write("test\n")

            with patch("entry.monitor.LOG_DIR", tmpdir):
                result = resolve_log_target("my_session")

            self.assertEqual(result, log_path)


if __name__ == "__main__":
    unittest.main()
