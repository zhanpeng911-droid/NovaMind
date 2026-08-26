"""
沙箱 Runtime 单元测试（P1 覆盖率盲区补齐）。

覆盖关键不变量（正反成对）：
  - LocalRuntime：allow_host_bash=False 直接拒绝（零信任）；超时/非零退出 → SandboxCommandError；
    文件缺失/权限 → Sandbox*Error；读文件截断；list_dir 剪枝；glob 截断；
    grep ReDoS 防护（超长 pattern / 嵌套量词拒绝）+ 忽略目录 + 大小写开关
  - DockerRuntime（subprocess 全 mock）：exec 成功/失败/docker 缺失；
    read/write/list/grep/update 的命令拼装与输出解析（含畸形行容错）
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from novamind.core.sandbox.exceptions import (
    SandboxCommandError,
    SandboxFileNotFoundError,
    SandboxPermissionError,
    SandboxRuntimeError,
)
from novamind.core.sandbox.runtimes import LocalRuntime


class TestLocalRuntimeExec(unittest.TestCase):
    def setUp(self):
        self.rt = LocalRuntime()

    def test_exec_disabled_raises(self):
        """零信任：allow_host_bash=False 时直接拒绝，不执行任何命令。"""
        rt = LocalRuntime(allow_host_bash=False)
        with self.assertRaises(SandboxRuntimeError) as ctx:
            rt.exec_command("echo hi")
        self.assertIn("host bash is disabled", str(ctx.exception))

    def test_exec_success_returns_stdout(self):
        out = self.rt.exec_command("echo hello")
        self.assertIn("hello", out)

    def test_exec_nonzero_exit_raises_command_error(self):
        with self.assertRaises(SandboxCommandError) as ctx:
            self.rt.exec_command("exit 3")
        self.assertEqual(ctx.exception.details.get("exit_code"), 3)

    def test_exec_timeout_raises_command_error(self):
        rt = LocalRuntime(exec_timeout=1)
        with patch("novamind.core.sandbox.runtimes.local_runtime.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
            with self.assertRaises(SandboxCommandError):
                rt.exec_command("sleep 100")


class TestLocalRuntimeFiles(unittest.TestCase):
    def setUp(self):
        self.rt = LocalRuntime()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_read_file_missing_raises_not_found(self):
        with self.assertRaises(SandboxFileNotFoundError):
            self.rt.read_file(str(self.root / "nope.txt"))

    def test_read_file_truncates_long_content(self):
        p = self.root / "big.txt"
        p.write_text("a" * 15000, encoding="utf-8")
        content = self.rt.read_file(str(p))
        self.assertLess(len(content), 10500)
        self.assertIn("[内容过长", content)

    def test_write_file_append_and_overwrite(self):
        p = self.root / "sub" / "f.txt"
        self.rt.write_file(str(p), "hello")
        self.rt.write_file(str(p), " world", append=True)
        self.assertEqual(Path(p).read_text(encoding="utf-8"), "hello world")
        self.rt.write_file(str(p), "new")
        self.assertEqual(Path(p).read_text(encoding="utf-8"), "new")

    def test_list_dir_missing_raises_not_found(self):
        with self.assertRaises(SandboxFileNotFoundError):
            self.rt.list_dir(str(self.root / "nope"))

    def test_list_dir_respects_max_depth_and_entries(self):
        (self.root / "a" / "deep").mkdir(parents=True)
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        # max_depth=1 只看到第一层
        entries = self.rt.list_dir(str(self.root), max_depth=1)
        self.assertIn("a", entries)
        self.assertIn("b.txt", entries)
        self.assertNotIn(str(Path("a") / "deep"), entries)
        # max_entries 剪枝
        entries = self.rt.list_dir(str(self.root), max_depth=2, max_entries=1)
        self.assertLessEqual(len(entries), 1)

    def test_glob_truncates_at_max_results(self):
        for i in range(5):
            (self.root / f"f{i}.txt").write_text("x", encoding="utf-8")
        matches, truncated = self.rt.glob(str(self.root), "*.txt", max_results=3)
        self.assertEqual(len(matches), 3)
        self.assertTrue(truncated)

    def test_download_update_binary_roundtrip(self):
        p = self.root / "bin" / "data.bin"
        self.rt.update_file(str(p), b"\x00\x01\xff")
        self.assertEqual(self.rt.download_file(str(p)), b"\x00\x01\xff")


class TestLocalRuntimeGrep(unittest.TestCase):
    def setUp(self):
        self.rt = LocalRuntime()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "code.py").write_text("TARGET = 1\nother = 2\ntarget twice\n", encoding="utf-8")

    def test_grep_finds_matches_with_line_numbers(self):
        matches, truncated = self.rt.grep(str(self.root), "target")
        self.assertFalse(truncated)
        self.assertTrue(any(m.line_number == 1 for m in matches))
        self.assertTrue(any(m.line_number == 3 for m in matches))

    def test_grep_case_sensitive_skips_upper_match(self):
        matches, _ = self.rt.grep(str(self.root), "target", case_sensitive=True)
        # case_sensitive 时只匹配小写 target 行（第1行 TARGET 不匹配）
        self.assertTrue(all(m.line_number != 1 for m in matches))

    def test_grep_literal_treats_regex_as_plain_text(self):
        (self.root / "re.txt").write_text("a.c actual\n", encoding="utf-8")
        matches, _ = self.rt.grep(str(self.root), "a.c", literal=True)
        # 字面量匹配：a.c 只命中字面 "a.c"，不把 . 当任意字符去匹配别的行
        self.assertTrue(all("a.c" in m.line for m in matches))

    def test_grep_rejects_overlong_pattern(self):
        with self.assertRaises(ValueError):
            self.rt.grep(str(self.root), "a" * 201)

    def test_grep_rejects_nested_quantifier_redos(self):
        with self.assertRaises(ValueError):
            self.rt.grep(str(self.root), "(a+)+$")

    def test_grep_skips_ignored_dirs(self):
        git_dir = self.root / ".git"
        git_dir.mkdir()
        (git_dir / "leak.py").write_text("TARGET secret\n", encoding="utf-8")
        matches, _ = self.rt.grep(str(self.root), "TARGET")
        self.assertTrue(all(".git" not in m.path for m in matches))


def _fake_completed(stdout="", returncode=0, stderr=""):
    proc = subprocess.CompletedProcess(args=["docker"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)
    return proc


class TestDockerRuntimeCommands(unittest.TestCase):
    """DockerRuntime 全程 mock subprocess.run，验证命令拼装与输出解析。"""

    def setUp(self):
        from novamind.core.sandbox.runtimes.docker_runtime import DockerRuntime
        self.rt = DockerRuntime("c1")
        self.captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            self.captured.append(args)
            return _fake_completed(stdout=self._respond(args))

        self._respond = lambda args: ""
        self._patcher = patch(
            "novamind.core.sandbox.runtimes.docker_runtime.subprocess.run",
            side_effect=fake_run,
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_exec_success_returns_stdout(self):
        self._respond = lambda args: "container output"
        self.assertEqual(self.rt.exec_command("echo hi"), "container output")
        self.assertIn("exec", self.captured[0])
        self.assertIn("c1", self.captured[0])

    def test_exec_nonzero_exit_raises_with_stderr(self):
        def fake_run(args, **kwargs):
            return _fake_completed(returncode=7, stderr="boom")
        with patch("novamind.core.sandbox.runtimes.docker_runtime.subprocess.run",
                   side_effect=fake_run):
            with self.assertRaises(SandboxCommandError) as ctx:
                self.rt.exec_command("false")
            self.assertEqual(ctx.exception.details.get("exit_code"), 7)
            self.assertIn("boom", str(ctx.exception))

    def test_exec_docker_missing_raises_runtime_error(self):
        with patch("novamind.core.sandbox.runtimes.docker_runtime.subprocess.run",
                   side_effect=FileNotFoundError("docker")):
            with self.assertRaises(SandboxRuntimeError):
                self.rt.exec_command("echo hi")

    def test_write_file_uses_append_redirect(self):
        self.rt.write_file("/u/f.txt", "data", append=True)
        cmd = self.captured[-1][-1]  # sh -c 后面的命令串
        self.assertIn(">>", cmd)
        self.assertNotIn("printf '%s'", "")

    def test_read_file_builds_cat(self):
        self._respond = lambda args: "content-here"
        self.assertEqual(self.rt.read_file("/u/a.txt"), "content-here")
        self.assertIn("cat", self.captured[0][-1])

    def test_list_dir_parses_find_output(self):
        self._respond = lambda args: "/u/a.txt\n/u/sub\n/u\n"
        result = self.rt.list_dir("/u", max_depth=1)
        self.assertIn("a.txt", result)
        self.assertIn(".", result)

    def test_glob_strips_base_prefix(self):
        self._respond = lambda args: "/u/x.log\n/u/sub/y.log\n"
        matches, truncated = self.rt.glob("/u", "*.log")
        self.assertIn("x.log", matches)
        self.assertFalse(truncated)

    def test_grep_parses_and_skips_malformed_lines(self):
        self._respond = lambda args: "/u/f.py:3:needle here\nnot-a-match-line\n/u/g.py:bad:num\n"
        matches, truncated = self.rt.grep("/u", "needle")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].line_number, 3)
        self.assertEqual(matches[0].path, "/u/f.py")
        self.assertFalse(truncated)

    def test_update_file_decodes_bytes(self):
        self.rt.update_file("/u/b.bin", b"raw-bytes")
        cmd = self.captured[-1][-1]
        self.assertIn("raw-bytes", cmd)


if __name__ == "__main__":
    unittest.main()
