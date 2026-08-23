"""
三组件沙箱 P1 测试。

覆盖关键不变量：
  - Sandbox 编排 validate → translate → execute → mask
  - LocalPathTranslator 翻译 + 脱敏 + 路径穿越拒绝
  - LocalSecurityGuard 路径穿越 / 白名单 / 危险命令 / shell 元字符 / 命令白名单
  - LocalSandboxProvider 确定性 ID + 复用
  - DockerPathGuard 写入路径白名单
  - WSL2 executor 路径翻译
"""
import os
import tempfile
import unittest
from unittest import mock

from novamind.core.sandbox import (
    Sandbox,
    LocalSandboxProvider,
    PathMapping,
    SandboxPermissionError,
    SandboxFileNotFoundError,
)
from novamind.core.sandbox.exceptions import SandboxRuntimeError
from novamind.core.sandbox.translators import LocalPathTranslator
from novamind.core.sandbox.guards import (
    LocalSecurityGuard,
    DockerPathGuard,
    AuditGuard,
)
from novamind.core.sandbox.runtimes import LocalRuntime
from novamind.core.sandbox.docker.executor import translate_to_wsl

VIRTUAL = "/mnt/novamind/user_data"


class _SandboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mapping = PathMapping(VIRTUAL, self.tmp.name)
        self.runtime = LocalRuntime()
        self.translator = LocalPathTranslator([self.mapping])
        self.guard = AuditGuard(LocalSecurityGuard([self.mapping]))
        self.sandbox = Sandbox("test", self.runtime, self.translator, self.guard)


class TestLocalPathTranslator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.translator = LocalPathTranslator([PathMapping(VIRTUAL, self.tmp.name)])

    def test_translate_path(self):
        physical = self.translator.translate_path(f"{VIRTUAL}/foo.txt")
        self.assertTrue(physical.endswith("foo.txt"))

    def test_mask_output_roundtrip(self):
        physical = self.translator.translate_path(f"{VIRTUAL}/foo.txt")
        masked = self.translator.mask_output(physical)
        self.assertEqual(masked, f"{VIRTUAL}/foo.txt")

    def test_translate_path_traversal_raises(self):
        with self.assertRaises(PermissionError):
            self.translator.translate_path(f"{VIRTUAL}/../evil.txt")


class TestLocalSecurityGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mapping = PathMapping(VIRTUAL, self.tmp.name)
        self.guard = LocalSecurityGuard([self.mapping])

    def test_validate_path_ok(self):
        self.guard.validate_path(f"{VIRTUAL}/foo.txt", write=True)

    def test_validate_path_traversal_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            self.guard.validate_path(f"{VIRTUAL}/../evil.txt")

    def test_validate_path_outside_whitelist_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            self.guard.validate_path("/etc/passwd")

    def test_write_readonly_rejected(self):
        ro = PathMapping("/mnt/novamind/skills", self.tmp.name, read_only=True)
        guard = LocalSecurityGuard([self.mapping, ro])
        with self.assertRaises(SandboxPermissionError):
            guard.validate_path("/mnt/novamind/skills/x", write=True)

    def test_shell_metachar_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            self.guard.validate_command("cat foo | grep bar")

    def test_env_expansion_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            self.guard.validate_command("echo $HOME")

    def test_command_whitelist(self):
        guard = LocalSecurityGuard([self.mapping], command_whitelist={"echo", "cat"})
        guard.validate_command("echo hello")  # 允许
        with self.assertRaises(SandboxPermissionError):
            guard.validate_command("python -c '...'")  # 拒绝

    def test_absolute_path_outside_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            self.guard.validate_command("cat /etc/passwd")


class TestSandboxOrchestration(_SandboxTestCase):
    def test_write_read_roundtrip(self):
        self.sandbox.write_file(f"{VIRTUAL}/test.txt", "hello")
        self.assertEqual(self.sandbox.read_file(f"{VIRTUAL}/test.txt"), "hello")

    def test_write_outside_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            self.sandbox.write_file("/etc/passwd", "x")

    def test_read_missing_file(self):
        with self.assertRaises(SandboxFileNotFoundError):
            self.sandbox.read_file(f"{VIRTUAL}/missing.txt")

    def test_list_dir(self):
        self.sandbox.write_file(f"{VIRTUAL}/a.txt", "a")
        self.sandbox.write_file(f"{VIRTUAL}/b.txt", "b")
        entries = self.sandbox.list_dir(VIRTUAL, max_depth=1)
        self.assertIn("a.txt", entries)
        self.assertIn("b.txt", entries)

    def test_get_host_path(self):
        host = self.sandbox.get_host_path(f"{VIRTUAL}/foo.txt")
        self.assertTrue(host.endswith("foo.txt"))


class TestLocalSandboxProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.provider = LocalSandboxProvider(
            path_mappings=[PathMapping(VIRTUAL, self.tmp.name)]
        )

    def test_acquire_deterministic_and_reuse(self):
        id1 = self.provider.acquire(thread_id="t1")
        id2 = self.provider.acquire(thread_id="t1")
        self.assertEqual(id1, id2)  # 同 thread 复用
        sandbox = self.provider.get(id1)
        self.assertIsNotNone(sandbox)

    def test_different_threads_different_ids(self):
        id1 = self.provider.acquire(thread_id="t1")
        id2 = self.provider.acquire(thread_id="t2")
        self.assertNotEqual(id1, id2)

    def test_write_via_provider(self):
        sid = self.provider.acquire(thread_id="t1")
        sb = self.provider.get(sid)
        sb.write_file(f"{VIRTUAL}/x.txt", "data")
        self.assertEqual(sb.read_file(f"{VIRTUAL}/x.txt"), "data")


class TestDockerPathGuard(unittest.TestCase):
    def test_write_under_mount_ok(self):
        DockerPathGuard().validate_path(f"{VIRTUAL}/x.txt", write=True)

    def test_write_outside_mount_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            DockerPathGuard().validate_path("/tmp/x.txt", write=True)

    def test_redirect_outside_mount_rejected(self):
        with self.assertRaises(SandboxPermissionError):
            DockerPathGuard().validate_command("echo hi > /tmp/x.txt")


class TestDefaultOfficeMapping(unittest.TestCase):
    def test_default_mapping_points_to_office(self):
        from novamind.core.sandbox.local import default_office_path_mappings
        from novamind.core.config import OFFICE_DIR

        mappings = default_office_path_mappings()
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].container_path, VIRTUAL)
        self.assertEqual(
            os.path.abspath(mappings[0].local_path),
            os.path.abspath(OFFICE_DIR),
        )


class TestWslExecutor(unittest.TestCase):
    def test_translate_drive(self):
        self.assertEqual(translate_to_wsl(r"D:\foo\bar"), "/mnt/d/foo/bar")

    def test_translate_forward_slash(self):
        self.assertEqual(translate_to_wsl("D:/foo/bar"), "/mnt/d/foo/bar")

    def test_non_windows_passthrough(self):
        self.assertEqual(translate_to_wsl("/mnt/data/x"), "/mnt/data/x")


class TestDockerRuntimeDownload(unittest.TestCase):
    def setUp(self):
        from novamind.core.sandbox.runtimes.docker_runtime import DockerRuntime

        self.rt = DockerRuntime("c1")

    def test_download_file_reads_copied_bytes(self):
        payload = b"hello docker \x00\x01\x02"

        def fake_run(args, **kwargs):
            # args = [docker, "cp", "c1:path", tmp_file]
            tmp_file = args[-1]
            with open(tmp_file, "wb") as f:
                f.write(payload)
            return None

        with mock.patch(
            "novamind.core.sandbox.runtimes.docker_runtime.subprocess.run",
            side_effect=fake_run,
        ):
            data = self.rt.download_file("/mnt/novamind/user_data/f.bin")

        self.assertEqual(data, payload)

    def test_download_file_docker_missing(self):
        with mock.patch(
            "novamind.core.sandbox.runtimes.docker_runtime.subprocess.run",
            side_effect=FileNotFoundError("docker"),
        ):
            with self.assertRaises(SandboxRuntimeError):
                self.rt.download_file("/mnt/novamind/user_data/f.bin")


class TestDockerWarmPool(unittest.TestCase):
    def setUp(self):
        from novamind.core.sandbox.docker.docker_sandbox_provider import DockerSandboxProvider

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # idle_timeout=0 关闭 idle checker 后台线程，避免测试线程泄漏
        self.provider = DockerSandboxProvider(
            self.tmp.name, warm_pool_size=2, idle_timeout=0,
        )
        self.create_count = 0

        def fake_create(sandbox_id):
            self.create_count += 1
            return f"novamind-{sandbox_id}"

        self.provider._create_container = fake_create
        self.addCleanup(self.provider.shutdown)

    def test_acquire_then_release_then_reuse_warm(self):
        sid = self.provider.acquire(thread_id="t1")
        self.assertEqual(self.create_count, 1)
        self.provider.release(sid)
        # 再次 acquire 复用 warm pool，不再次 docker run
        sid2 = self.provider.acquire(thread_id="t1")
        self.assertEqual(sid2, sid)
        self.assertEqual(self.create_count, 1)

    def test_active_acquire_returns_same_without_create(self):
        sid = self.provider.acquire(thread_id="t1")
        sid2 = self.provider.acquire(thread_id="t1")
        self.assertEqual(sid, sid2)
        self.assertEqual(self.create_count, 1)

    def test_different_threads_get_distinct_sandboxes(self):
        sid_a = self.provider.acquire(thread_id="t1")
        sid_b = self.provider.acquire(thread_id="t2")
        self.assertNotEqual(sid_a, sid_b)
        self.assertEqual(self.create_count, 2)

    def test_warm_pool_capacity_evicts_oldest(self):
        # 三个线程，容量 2：第三个 release 时淘汰最旧
        s1 = self.provider.acquire(thread_id="t1")
        s2 = self.provider.acquire(thread_id="t2")
        s3 = self.provider.acquire(thread_id="t3")
        self.provider.release(s1)
        self.provider.release(s2)
        self.provider.release(s3)  # 超限，淘汰最旧 warm
        # 再次 acquire t1 需重新创建（已被淘汰）
        self.provider.acquire(thread_id="t1")
        self.assertGreaterEqual(self.create_count, 4)


if __name__ == "__main__":
    unittest.main()
