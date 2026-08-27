"""P3-1 回归：记忆存储收敛统一数据根 + 旧数据一次性迁移。

不变量：
  I1 相对 storage_path 解析到 WORKSPACE_DIR 下，与 CWD 无关（跨目录启动同位置）
  I2 显式绝对路径仍优先（保留配置覆盖）
  I3 旧版 CWD/.novamind/memory 有数据且新位置为空 → 迁移到新位置
  I4 新位置已有数据 → 不覆盖不迁移
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from novamind.core.memory.strategies.default.strategy import (
    _migrate_legacy_storage,
    _resolve_storage_root,
    build_default_provider,
)


class TestResolveStorageRoot(unittest.TestCase):
    def test_relative_resolves_under_workspace_regardless_of_cwd(self):
        ws = "C:/data/novamind_ws"
        with patch("novamind.core.memory.strategies.default.strategy.WORKSPACE_DIR", ws), \
                patch.object(Path, "cwd", return_value=Path("C:/elsewhere")):
            root = _resolve_storage_root("memory_traces")
        self.assertEqual(root, Path("C:/data/novamind_ws") / "memory_traces")

    def test_absolute_path_kept_verbatim(self):
        with patch("novamind.core.memory.strategies.default.strategy.WORKSPACE_DIR", "X"):
            root = _resolve_storage_root("/var/lib/novamind/mem")
        self.assertEqual(root, Path("/var/lib/novamind/mem"))


class TestLegacyMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.legacy = self.root / ".novamind" / "memory"
        self.target = self.root / "ws" / "memory_traces"

    def test_migrates_when_target_empty(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "traces.md").write_text("# Memory Traces\n\n## t1\n", encoding="utf-8")
        with patch.object(Path, "cwd", return_value=self.root):
            _migrate_legacy_storage(self.target)
        self.assertFalse(self.legacy.exists())
        self.assertTrue((self.target / "traces.md").exists())

    def test_no_migration_when_target_has_data(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / "traces.md").write_text("legacy", encoding="utf-8")
        self.target.mkdir(parents=True)
        (self.target / "traces.md").write_text("newer", encoding="utf-8")
        with patch.object(Path, "cwd", return_value=self.root):
            _migrate_legacy_storage(self.target)
        self.assertTrue(self.legacy.exists())  # 不动旧数据
        self.assertEqual((self.target / "traces.md").read_text(encoding="utf-8"), "newer")

    def test_no_legacy_noop(self):
        with patch.object(Path, "cwd", return_value=self.root):
            _migrate_legacy_storage(self.target)  # 不抛即通过
        self.assertFalse(self.target.exists())


class TestBuildProviderStoragePlacement(unittest.TestCase):
    def test_build_places_traces_under_workspace_not_cwd(self):
        """端到端：跨 CWD 启动，traces 落在统一数据根，而非当前目录。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ws = Path(self.tmp.name) / "ws"
        ws.mkdir()
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        from novamind.core.memory.config import MemoryConfig, set_memory_config
        old = get_memory_config()
        set_memory_config(MemoryConfig(storage_path="memory_traces"))
        try:
            with patch("novamind.core.memory.strategies.default.strategy.WORKSPACE_DIR",
                       str(ws)), \
                    patch.object(Path, "cwd", return_value=elsewhere):
                provider = build_default_provider()
            self.assertTrue((ws / "memory_traces" / "traces.md").exists())
            self.assertFalse((elsewhere / "memory_traces").exists())
            self.assertIsNotNone(provider._store)
        finally:
            set_memory_config(old)


from novamind.core.memory.config import get_memory_config  # noqa: E402

if __name__ == "__main__":
    unittest.main()


class TestRecallIndexWiring(unittest.TestCase):
    """缺陷#1 回归：build_default_provider 默认路径必须把 encode 后的记忆接进检索索引。"""

    def test_encode_then_retrieve_hits(self):
        import tempfile as _tf
        from novamind.core.memory.config import MemoryConfig, get_memory_config as _gm, set_memory_config as _sm
        from novamind.core.memory.strategies.default.strategy import build_default_provider
        from novamind.core.memory.schema import MemoryType
        from novamind.core.memory.types import MemoryQuery

        tmp = _tf.TemporaryDirectory()
        old = _gm()
        _sm(MemoryConfig(storage_path=str(Path(tmp.name) / "mem")))
        try:
            provider = build_default_provider()
            provider._manager.encode("用户的小名叫豆豆", type=MemoryType.SEMANTIC, importance=0.9)
            hits = provider.retriever().retrieve(MemoryQuery(text="我的小名叫什么"))
            self.assertGreaterEqual(len(hits), 1, "encode 后未进入检索索引（缺陷#1 复发）")
        finally:
            _sm(old)
            tmp.cleanup()

    def test_wrap_store_is_idempotent(self):
        import tempfile as _tf
        from novamind.core.memory.config import MemoryConfig, get_memory_config as _gm, set_memory_config as _sm
        from novamind.core.memory.strategies.default.strategy import build_default_provider
        from novamind.core.memory.schema import MemoryType
        from novamind.core.memory.types import MemoryQuery

        tmp = _tf.TemporaryDirectory()
        old = _gm()
        _sm(MemoryConfig(storage_path=str(Path(tmp.name) / "mem")))
        try:
            p1 = build_default_provider()
            self.assertIn(id(p1._retriever), p1._store._wired_retrievers)
            # 二次调用 build（同 store 场景）不会重复包装
            p2 = build_default_provider(store=p1._store)
            self.assertIn(id(p2._retriever), p2._store._wired_retrievers)
            p2._manager.encode("重复包装检测", type=MemoryType.EPISODIC, importance=0.5)
            hits = p2.retriever().retrieve(MemoryQuery(text="重复包装检测"))
            self.assertGreaterEqual(len(hits), 1)
        finally:
            _sm(old)
            tmp.cleanup()
