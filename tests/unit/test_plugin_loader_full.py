"""
插件加载器测试（P1-B3：懒加载/热更新/生命周期钩子/零信任 run 限制）。

覆盖关键不变量（正反成对）：
  - 扫描：四种描述文件名优先级、非目录跳过、无 md 跳过、缓存命中、
    force_rescan、重名首胜、坏文件隔离（不拖垮扫描）
  - 元数据：name/desc/version 提取、引号 desc 剥离、回退默认值、目录名回退
  - run_dir：目录在 office 内给相对路径，否则 None（零信任：run 被禁）
  - 懒工具：help 触发 on_load 并返回说明书；run 缺 command 拒绝、
    不在 office 拒绝（沙盒安全）、合法 run 转交 shell；非法 mode 拒绝
  - 生命周期：disable/enable、on_unload 钩子、reload_all 清缓存重扫、
    add_scan_dir 去重、便捷函数接线
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from novamind.core.plugin_loader import PluginManager


class _Base(unittest.TestCase):
    """公共布局：skills 建在 office 内（对齐 OFFICE_DIR/skills），另设 office 外目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.office = self.root / "office"
        self.skills = self.office / "skills"
        self.skills.mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self._patch_skill = patch("novamind.core.plugin_loader.SKILLS_DIR", str(self.skills))
        self._patch_skill.start()
        self.addCleanup(self._patch_skill.stop)
        self._patch_office = patch("novamind.core.plugin_loader.OFFICE_DIR", str(self.office))
        self._patch_office.start()
        self.addCleanup(self._patch_office.stop)
        self.pm = PluginManager(scan_interval=3600)

    def _write_skill(self, folder: str, content: str, base: Path | None = None,
                     filename: str = "SKILL.md") -> Path:
        base = base or self.skills
        f = (base / folder) / filename
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        return f


class TestPluginManagerScan(_Base):
    def test_scan_priority_skil_md_over_readme(self):
        self._write_skill("alpha", "description: from readme\n", filename="README.md")
        self._write_skill("alpha", "name: alpha\ndescription: from skill\n")
        plugins = self.pm._scan_plugins()
        self.assertEqual(plugins["alpha"].description, "from skill")

    def test_scan_ignores_non_dir_and_mdless_dir(self):
        (self.skills / "plain.txt").write_text("x", encoding="utf-8")
        (self.skills / "empty").mkdir()
        plugins = self.pm._scan_plugins()
        self.assertNotIn("empty", plugins)

    def test_scan_cache_hit_and_force_rescan(self):
        self._write_skill("alpha", "name: alpha\ndescription: d\n")
        first = self.pm._scan_plugins()
        second = self.pm._scan_plugins()          # 缓存命中
        self.assertIs(first, second)
        forced = self.pm._scan_plugins(force_rescan=True)
        self.assertIsNot(first, forced)

    def test_metadata_extraction_and_fallbacks(self):
        self._write_skill("alpha", 'name: my-skill\ndescription: "带引号描述"\nversion: 2.1.0\n')
        meta = self.pm._extract_metadata(str(self.skills / "alpha" / "SKILL.md"))
        self.assertEqual(meta["name"], "my-skill")
        self.assertEqual(meta["description"], "带引号描述")  # 引号剥离
        self.assertEqual(meta["version"], "2.1.0")

        self._write_skill("alpha", "x y\n", filename="README.md")
        meta2 = self.pm._extract_metadata(str(self.skills / "alpha" / "README.md"))
        self.assertEqual(meta2["raw_name"], "alpha")
        self.assertIn("alpha", meta2["description"])
        self.assertEqual(meta2["version"], "1.0.0")

    def test_metadata_bad_file_returns_none(self):
        self.assertIsNone(self.pm._extract_metadata(str(self.root / "ghost.md")))

    def test_bad_plugin_does_not_break_scan(self):
        self._write_skill("badone", "\x00\x00 garbage" * 5)
        plugins = self.pm._scan_plugins()  # 不抛即通过（坏插件被隔离）
        self.assertIsInstance(plugins, dict)

    def test_run_dir_inside_office_only(self):
        self._write_skill("alpha", "name: alpha\ndescription: in office\n")
        self._write_skill("ext", "name: ext\ndescription: outside\n", base=self.outside)
        self.pm.add_scan_dir(str(self.outside))
        plugins = self.pm._scan_plugins()
        self.assertEqual(plugins["alpha"].run_dir, "skills/alpha")  # office 内 → 相对工位路径
        self.assertIsNone(plugins["ext"].run_dir)            # office 外 → None

    def test_duplicate_name_first_wins(self):
        self._write_skill("alpha", "name: dup\ndescription: first\n")
        self._write_skill("extra", "name: dup\ndescription: second\n")
        plugins = self.pm._scan_plugins()
        self.assertEqual(plugins["dup"].description, "first")


class TestPluginManagerTools(_Base):
    def setUp(self):
        super().setUp()
        self._write_skill("alpha", "name: alpha\ndescription: 工具\n")
        self._write_skill("beta", "name: beta\ndescription: 工具2\n")
        self._write_skill("ext", "name: ext\ndescription: 外部\n", base=self.outside)
        self.pm.add_scan_dir(str(self.outside))
        # 桩掉 shell 执行
        self.shell = MagicMock()
        self.shell.invoke.return_value = " [STDOUT] ok"
        self.patch_shell = patch("novamind.core.plugin_loader.execute_office_shell", self.shell)
        self.patch_shell.start()
        self.addCleanup(self.patch_shell.stop)

    def _tool(self, name="alpha"):
        tools = {t.name: t for t in self.pm.get_all_tools()}
        return tools[name]

    def test_lazy_help_fires_on_load_and_returns_content(self):
        loaded = []
        self.pm.register_hook("on_load", lambda p: loaded.append(p.name))
        out = self._tool("alpha").invoke({"mode": "help"})
        self.assertIn("说明书", out)
        self.assertIn("alpha", out)
        self.assertEqual(loaded, ["alpha"])

    def test_run_without_command_rejected(self):
        out = self._tool("alpha").invoke({"mode": "run"})
        self.assertIn("必须提供 command", out)

    def test_run_outside_office_rejected(self):
        out = self._tool("ext").invoke({"mode": "run", "command": "ls"})
        self.assertIn("沙盒安全限制", out)
        self.assertFalse(self.shell.called)  # 零信任：不执行

    def test_run_inside_office_substitutes_basedir_and_executes(self):
        out = self._tool("beta").invoke(
            {"mode": "run", "command": "ls {baseDir}/"})
        self.assertEqual(out, " [STDOUT] ok")
        cmd = self.shell.invoke.call_args.args[0]["command"]
        self.assertIn("skills/beta", cmd)   # {baseDir} 替换为相对工位路径
        self.assertNotIn("{baseDir}", cmd)

    def test_invalid_mode_rejected(self):
        self.assertIn("只能是 'help' 或 'run'",
                      self._tool("alpha").invoke({"mode": "foo"}))

    def test_hook_exception_tolerated(self):
        def boom(p):
            raise RuntimeError("hook failed")
        self.pm.register_hook("on_load", boom)
        out = self._tool("alpha").invoke({"mode": "help"})
        self.assertIn("说明书", out)  # 钩子异常不阻断

    def test_disable_unknown_false_and_known_fires_on_unload(self):
        unloaded = []
        self.pm.register_hook("on_unload", lambda p: unloaded.append(p.name))
        self.assertFalse(self.pm.disable_plugin("ghost"))
        self.assertTrue(self.pm.disable_plugin("alpha"))
        self.assertEqual(unloaded, ["alpha"])
        self.assertNotIn("alpha", {t.name for t in self.pm.get_all_tools()})

    def test_enable_plugin(self):
        self.pm.disable_plugin("alpha")
        self.assertTrue(self.pm.enable_plugin("alpha"))
        self.assertIn("alpha", {t.name for t in self.pm.get_all_tools()})

    def test_reload_all_clears_cache_and_rescans(self):
        self.pm.get_all_tools()
        before = len(self.pm.list_plugins())
        tools = self.pm.reload_all()  # 回归：lru_cache 绑定方法无 cache_clear 的 bug
        self.assertEqual(len(self.pm.list_plugins()), before)
        self.assertIsInstance(tools, list)

    def test_counts_and_listing(self):
        self.assertGreaterEqual(self.pm.get_plugin_count(), 2)
        self.assertGreaterEqual(self.pm.get_enabled_count(), 2)
        listing = self.pm.list_plugins()
        self.assertIn("name", listing[0])
        self.assertIn("run_dir", listing[0])

    def test_add_scan_dir_dedupes(self):
        before = len(self.pm._extra_scan_dirs)  # setUp 已加 self.outside
        self.pm.add_scan_dir(str(self.outside))  # 重复 → 不追加
        self.assertEqual(len(self.pm._extra_scan_dirs), before)
        self.pm.add_scan_dir("~/.nonexistent-skills")
        self.assertEqual(len(self.pm._extra_scan_dirs), before + 1)


class TestModuleConvenience(unittest.TestCase):
    def test_load_dynamic_skills_and_reload_wire_to_singleton(self):
        import novamind.core.plugin_loader as pl
        with patch.object(pl._plugin_manager, "get_all_tools",
                          return_value=["t"]) as ga, \
                patch.object(pl._plugin_manager, "reload_all",
                             return_value=["t"]) as ra:
            self.assertEqual(pl.load_dynamic_skills(), ["t"])
            self.assertEqual(pl.reload_skills(), ["t"])
            ga.assert_called_once_with(force_rescan=False)
            ra.assert_called_once()
        with patch.object(pl._plugin_manager, "get_plugin_count", return_value=2):
            self.assertEqual(pl.get_skill_count(), 2)
        # cache_clear 在类级函数包装器上（实例访问是绑定方法，无该属性）
        with patch.object(pl.PluginManager._load_content, "cache_clear") as cc:
            pl.clear_skill_cache()
            cc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
