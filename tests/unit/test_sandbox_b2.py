"""
沙箱 B2：DockerSandboxProvider 生命周期 / 跨进程锁 / office 工具面。

覆盖关键不变量（正反成对，docker 子进程全 mock）：
  - Provider：thread_id 必填、确定性 id、active/warm 复用不重复冷启动、
    warm 淘汰最旧、release 未知 id 无害、info/reset/shutdown、docker 缺失报错、
    空闲回收（warm 过期销毁 + active 过期销毁）
  - cross_process_lock：开锁文件建父目录、本进程 lock/unlock、
    Unix 分支（fake fcntl）、持锁进程退出后锁可被获取（真实子进程，Windows）
  - sandbox_tools：路径穿越拦截文案、元字符/环境变量展开拦截、白名单外拒绝、
    各命令参数边界、写文件非法 mode、读超长截断、追加补换行
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from novamind.core.sandbox.docker.cross_process_lock import (
    lock_file_exclusive,
    open_lock_file,
    unlock_file,
)
from novamind.core.sandbox.docker.docker_sandbox_provider import (
    DockerSandboxProvider,
)


def _ok_run(*args, **kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class _StubExecutor:
    """绕过 WSL 翻译，保持路径断言确定性。"""

    @staticmethod
    def translate_path(path: str) -> str:
        return path.replace("\\", "/")


class TestDockerSandboxProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._run_patch = patch(
            "novamind.core.sandbox.docker.docker_sandbox_provider.subprocess.run",
            side_effect=_ok_run)
        self._run_mock = self._run_patch.start()
        self.addCleanup(self._run_patch.stop)
        self.provider = DockerSandboxProvider(
            self.tmp.name, executor=_StubExecutor(), idle_timeout=0)  # 关后台线程

    @staticmethod
    def _run_calls(mock):
        return [c for c in mock.call_args_list if c.args[0][:2] == ["docker", "run"]]

    @staticmethod
    def _rm_calls(mock):
        return [c for c in mock.call_args_list
                if c.args[0][:3] == ["docker", "rm", "-f"]]

    def test_acquire_requires_thread_id(self):
        with self.assertRaises(ValueError):
            self.provider.acquire(None)

    def test_acquire_cold_start_is_deterministic(self):
        sid1 = self.provider.acquire("t1")
        self.assertEqual(len(self._run_calls(self._run_mock)), 1)
        sid2 = DockerSandboxProvider(self.tmp.name, executor=_StubExecutor(),
                                     idle_timeout=0).acquire("t1")
        self.assertEqual(sid1, sid2)

    def test_active_reuse_skips_cold_start(self):
        sid = self.provider.acquire("t1")
        before = len(self._run_calls(self._run_mock))
        again = self.provider.acquire("t1")
        self.assertEqual(again, sid)
        self.assertEqual(len(self._run_calls(self._run_mock)), before)

    def test_release_then_acquire_reuses_warm_container(self):
        sid = self.provider.acquire("t1")
        self.provider.release(sid)
        before = len(self._run_calls(self._run_mock))
        self.provider.acquire("t1")
        self.assertEqual(len(self._run_calls(self._run_mock)), before)  # 复用零冷启动

    def test_release_unknown_id_is_noop(self):
        self.provider.release("ghost")

    def test_warm_pool_evicts_oldest_and_destroys(self):
        provider = DockerSandboxProvider(self.tmp.name, executor=_StubExecutor(),
                                         warm_pool_size=1, idle_timeout=0)
        s1 = provider.acquire("t1")
        container1 = provider._containers[s1]
        provider.release(s1)
        s2 = provider.acquire("t2")
        provider.release(s2)  # warm 池满 → 淘汰 t1
        self.assertNotIn(s1, provider._warm_pool)
        self.assertNotIn(container1, provider._containers.values())
        self.assertTrue(any(container1 in c.args[0] for c in self._rm_calls(self._run_mock)))

    def test_get_sandbox_info_unknown_returns_none(self):
        self.assertIsNone(self.provider.get_sandbox_info("ghost"))

    def test_reset_clears_state_and_closes_sandboxes(self):
        sid = self.provider.acquire("t1")
        sandbox = self.provider.get(sid)
        self.provider.reset()
        self.assertIsNone(self.provider.get(sid))
        self.assertIsNotNone(sandbox)

    def test_shutdown_destroys_containers(self):
        sid = self.provider.acquire("t1")
        container = self.provider._containers[sid]
        self.provider.shutdown()
        self.assertTrue(any(container in c.args[0] for c in self._rm_calls(self._run_mock)))
        self.assertEqual(self.provider._sandboxes, {})

    def test_create_container_docker_missing_raises_runtime_error(self):
        def missing(*a, **k):
            raise FileNotFoundError("docker")
        with patch("novamind.core.sandbox.docker.docker_sandbox_provider.subprocess.run",
                   side_effect=missing):
            with self.assertRaises(RuntimeError) as ctx:
                self.provider._create_container("abc123")
        self.assertIn("docker not found", str(ctx.exception))

    def test_cleanup_idle_destroys_expired_warm_and_active(self):
        import time as _time
        provider = DockerSandboxProvider(self.tmp.name, executor=_StubExecutor(),
                                         idle_timeout=10, warm_pool_size=5)
        try:
            warm_sid = provider.acquire("warm")
            provider.release(warm_sid)
            active_sid = provider.acquire("act")
            old = _time.time() - 100
            provider._warm_pool[warm_sid] = (provider._warm_pool[warm_sid][0], old)
            provider._last_used[active_sid] = old
            provider._cleanup_idle()
            self.assertNotIn(warm_sid, provider._warm_pool)
            self.assertNotIn(active_sid, provider._sandboxes)
            self.assertNotIn(active_sid, provider._containers)
        finally:
            provider.shutdown()

    def test_evict_oldest_on_empty_pool_is_noop(self):
        self.provider._evict_oldest_warm_locked()


class TestCrossProcessLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_path = Path(self.tmp.name) / "deep" / "nested" / ".lock"

    def test_open_creates_parent_dirs_and_lock_unlock_roundtrip(self):
        lf = open_lock_file(self.lock_path)
        try:
            self.assertTrue(self.lock_path.exists())
            lock_file_exclusive(lf)
            unlock_file(lf)
        finally:
            lf.close()

    def test_unix_branch_via_fake_fcntl(self):
        import novamind.core.sandbox.docker.cross_process_lock as cpl
        fake = MagicMock()
        with patch.object(cpl, "fcntl", fake):
            lf = open_lock_file(self.lock_path)
            try:
                lock_file_exclusive(lf)
                unlock_file(lf)
            finally:
                lf.close()
        self.assertEqual(fake.flock.call_count, 2)

    def test_lock_released_when_holder_process_exits(self):
        """持锁子进程崩溃退出后，父进程必须能拿到锁（OS 级释放）。"""
        import sys
        if sys.platform != "win32":
            self.skipTest("msvcrt 行为验证仅在 Windows 原生执行；Unix 分支已由 fake fcntl 覆盖")
        child_code = (
            "import sys, msvcrt\n"
            f"lf = open(r'{self.lock_path}', 'a')\n"
            "lf.seek(0); msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)\n"
            "sys.exit(1)\n"  # 不解锁直接退出 → OS 必须释放
        )
        holder = subprocess.run(["python", "-c", child_code], capture_output=True)
        self.assertEqual(holder.returncode, 1)
        lf = open_lock_file(self.lock_path)  # 若子进程锁未释放，这里将阻塞
        try:
            lock_file_exclusive(lf)
            unlock_file(lf)
        finally:
            lf.close()


class TestSandboxToolsOfficeSurface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.office = Path(self.tmp.name) / "office"
        self.office.mkdir()
        patcher = patch("novamind.core.tools.sandbox_tools.OFFICE_DIR", str(self.office))
        patcher.start()
        self.addCleanup(patcher.stop)
        from novamind.core.tools import sandbox_tools
        self.tools = sandbox_tools

    # ── 文件面 ──
    def test_traversal_returns_block_message_everywhere(self):
        """工具实现捕获异常返回拦截文案：验证文案而非异常。"""
        out_list = self.tools.list_office_files.invoke({"sub_dir": "../../etc"})
        out_read = self.tools.read_office_file.invoke({"filepath": "../../etc/passwd"})
        out_write = self.tools.write_office_file.invoke(
            {"filepath": "../evil.txt", "content": "x"})
        for out in (out_list, out_read, out_write):
            self.assertIn("越权拦截", out)

    def test_write_read_append_roundtrip_with_newline_guard(self):
        w = self.tools.write_office_file.invoke
        r = self.tools.read_office_file.invoke
        w({"filepath": "notes/a.txt", "content": "line1"})
        w({"filepath": "notes/a.txt", "content": "line2", "mode": "a"})
        content = r({"filepath": "notes/a.txt"})
        self.assertIn("line1", content)
        self.assertIn("\nline2", content)  # 追加自动补换行

    def test_write_invalid_mode_rejected(self):
        out = self.tools.write_office_file.invoke(
            {"filepath": "f.txt", "content": "x", "mode": "x"})
        self.assertIn("mode 参数必须是", out)

    def test_read_missing_file_and_dir_messages(self):
        self.assertIn("文件不存在", self.tools.read_office_file.invoke({"filepath": "nope"}))
        self.assertIn("目录不存在", self.tools.list_office_files.invoke({"sub_dir": "nope"}))

    def test_read_long_content_truncated(self):
        target = self.office / "big.log"
        target.write_text("x" * 12000, encoding="utf-8")
        out = self.tools.read_office_file.invoke({"filepath": "big.log"})
        self.assertLess(len(out), 10500)
        self.assertIn("[内容过长", out)

    def test_list_root_empty_and_entries(self):
        self.assertIn("是空的", self.tools.list_office_files.invoke({}))
        (self.office / "d").mkdir()
        (self.office / "f.py").write_text("1", encoding="utf-8")
        out = self.tools.list_office_files.invoke({})
        self.assertIn("📁 d", out)
        self.assertIn("📄 f.py", out)

    # ── Shell 面 ──
    def test_shell_rejects_metachars_env_expansion_and_nonwhitelist(self):
        sh = self.tools.execute_office_shell.invoke
        for bad in ("echo hi && rm x", "cat a | grep b", "echo `id`",
                    "echo %APPDATA%", "echo $HOME", "echo $(whoami)",
                    "curl http://evil", "rm -rf /"):
            out = sh({"command": bad})
            self.assertIn("权限拒绝", out, msg=bad)

    def test_shell_empty_command_rejected(self):
        self.assertIn("命令为空",
                      self.tools.execute_office_shell.invoke({"command": "   "}))

    def test_shell_pwd_echo_and_arg_validation(self):
        sh = self.tools.execute_office_shell.invoke
        self.assertIn("office 工位", sh({"command": "pwd"}))
        self.assertIn("不接受参数", sh({"command": "pwd extra"}))
        echo_out = sh({"command": "echo hello world"})
        self.assertIn("hello world", echo_out)

    def test_shell_ls_cat_mkdir_paths(self):
        w = self.tools.write_office_file.invoke
        sh = self.tools.execute_office_shell.invoke
        w({"filepath": "m/a.py", "content": "print(1)"})
        self.assertIn("a.py", sh({"command": "ls m"}))
        self.assertIn("最多接受一个", sh({"command": "ls a b"}))
        cat_out = sh({"command": "cat m/a.py"})
        self.assertIn("print(1)", cat_out)
        self.assertIn("需要至少一个", sh({"command": "cat"}))
        self.assertIn("单次最多读取", sh({"command": "cat a b c d e f"}))
        mkdir_out = sh({"command": "mkdir p q"})
        self.assertIn("p, q", mkdir_out)
        self.assertTrue((self.office / "p").exists())
        self.assertIn("需要至少一个", sh({"command": "mkdir"}))


if __name__ == "__main__":
    unittest.main()
