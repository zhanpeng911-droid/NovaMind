"""
NovaMind plugin loader tests.
"""
import os
import shutil
import unittest

from novamind.core.config import SKILLS_DIR
from novamind.core.plugin_loader import PluginManager


class TestPluginLoader(unittest.TestCase):
    """测试动态插件路径处理"""

    def setUp(self):
        self.plugin_dir = os.path.join(SKILLS_DIR, "review_path_plugin")
        os.makedirs(self.plugin_dir, exist_ok=True)
        with open(os.path.join(self.plugin_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(
                "name: review-path-plugin\n"
                "description: Review path plugin\n\n"
                "Run commands from {baseDir}.\n"
            )

    def tearDown(self):
        shutil.rmtree(self.plugin_dir, ignore_errors=True)

    def test_base_dir_uses_real_office_relative_path(self):
        manager = PluginManager(scan_interval=0)
        plugins = manager._scan_plugins(force_rescan=True)

        plugin = plugins["review-path-plugin"]
        self.assertEqual(plugin.run_dir.replace("\\", "/"), "skills/review_path_plugin")


if __name__ == "__main__":
    unittest.main()
