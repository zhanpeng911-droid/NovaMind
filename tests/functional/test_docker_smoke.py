"""真实 Docker 沙箱 smoke test（opt-in，加固 Phase 7）。

不在普通 CI 中运行（tests/functional 已被 CI --ignore 排除）。运行方式：

    pytest tests/functional/test_docker_smoke.py -v

前置条件（不可用时自动 skip，不算失败）：
- docker daemon 可用；
- novamind-sandbox:latest 镜像已构建（仓库当前没有镜像定义，
  构建方式由运维决定——这正是"未实测不宣称"的原因）。

覆盖：创建、挂载区写入/读回、release/reacquire、拒绝 mount 外写。
"""
import os
import shutil
import tempfile
import unittest

from novamind.core.sandbox.docker import DockerSandboxProvider

DOCKER_AVAILABLE = None
IMAGE = os.getenv("NOVAMIND_DOCKER_IMAGE", "novamind-sandbox:latest")


def _docker_ready() -> bool:
    global DOCKER_AVAILABLE
    if DOCKER_AVAILABLE is None:
        probe = os.system("docker version > /dev/null 2>&1")
        DOCKER_AVAILABLE = probe == 0
        if DOCKER_AVAILABLE:
            check = os.system(f'docker image inspect "{IMAGE}" > /dev/null 2>&1')
            DOCKER_AVAILABLE = check == 0
    return DOCKER_AVAILABLE


@unittest.skipUnless(_docker_ready(), f"docker daemon 或镜像 {IMAGE} 不可用")
class TestRealDockerSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_root = tempfile.mkdtemp(prefix="novamind_docker_smoke_")
        cls.provider = DockerSandboxProvider(cls.tmp_root, image=IMAGE)

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        shutil.rmtree(cls.tmp_root, ignore_errors=True)

    def test_01_create_and_mount_zone_roundtrip(self):
        sid = self.provider.acquire("smoke_thread")
        sandbox = self.provider.get(sid)
        self.assertIsNotNone(sandbox)
        # 挂载区内写入/读回
        sandbox.write_file("/mnt/novamind/user_data/smoke/a.txt", "hello docker")
        content = sandbox.read_file("/mnt/novamind/user_data/smoke/a.txt")
        self.assertEqual(content, "hello docker")
        type(self).last_sid = sid

    def test_02_release_and_reacquire(self):
        sid = getattr(self, "last_sid", None) or self.provider.acquire("smoke_thread")
        self.provider.release(sid)
        # release 不销毁（warm pool 语义）：reacquire 可复用同一 sandbox
        sid2 = self.provider.acquire("smoke_thread")
        self.assertEqual(sid, sid2)
        sandbox = self.provider.get(sid2)
        self.assertIsNotNone(sandbox)

    def test_03_write_outside_mount_rejected(self):
        sid = self.provider.acquire("smoke_thread")
        sandbox = self.provider.get(sid)
        from novamind.core.sandbox.exceptions import SandboxError

        with self.assertRaises((SandboxError, PermissionError, ValueError, RuntimeError)):
            sandbox.write_file("/etc/novamind_escape.txt", "should fail")


if __name__ == "__main__":
    unittest.main()
