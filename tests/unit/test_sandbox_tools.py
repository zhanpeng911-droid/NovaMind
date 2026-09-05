"""
NovaMind sandbox tool security tests.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from novamind.core.config import WORKSPACE_DIR
from novamind.core.tools.sandbox_tools import (
    execute_office_shell,
    write_office_file,
)


class TestSandboxTools(unittest.TestCase):
    """测试 office 沙盒边界"""

    def test_rejects_commonpath_prefix_bypass(self):
        result = write_office_file.invoke({
            "filepath": "../office2/escape.txt",
            "content": "escaped",
        })

        self.assertIn("越权拦截", result)
        self.assertFalse(
            os.path.exists(os.path.join(WORKSPACE_DIR, "office2", "escape.txt"))
        )

    def test_controlled_shell_rejects_interpreter_escape(self):
        result = execute_office_shell.invoke({
            "command": "python -c print('escape')",
        })

        self.assertIn("权限拒绝", result)

    def test_controlled_shell_allows_safe_listing(self):
        result = execute_office_shell.invoke({"command": "dir"})

        self.assertIn("当前系统", result)
        self.assertIn("[STDOUT]", result)


if __name__ == "__main__":
    unittest.main()


class TestOfficeToolsGoldenContract(unittest.TestCase):
    """加固 Phase 0：冻结四个 office 工具的公开契约。

    本类断言的是"当前对外的样子"。Phase 1 把实现迁移到 provider-backed façade 时
    必须逐条保持；有意变更时先改本类并在 commit message 里说明。
    """

    OFFICE_TOOLS = (
        "list_office_files", "read_office_file", "write_office_file", "execute_office_shell",
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.office = Path(tmp.name) / "office"
        self.office.mkdir()
        patcher = patch("novamind.core.tools.sandbox_tools.OFFICE_DIR", str(self.office))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tool(self, name):
        from novamind.core.tools import sandbox_tools as st
        return getattr(st, name)

    # ── 名称 / 导出 / 注册表 ──
    def test_golden_names_exports_and_registry(self):
        from novamind.core.tools import (
            execute_office_shell as exported_shell,
            list_office_files as exported_list,
            read_office_file as exported_read,
            write_office_file as exported_write,
        )
        import novamind.core.plugin_loader as pl
        from novamind.core.tools import sandbox_tools as st
        from novamind.core.tools.builtins import BUILTIN_TOOLS
        from novamind.core.policy import DEFAULT_POLICY

        for name in self.OFFICE_TOOLS:
            self.assertIn(name, {t.name for t in BUILTIN_TOOLS},
                          f"{name} 必须保留在 BUILTIN_TOOLS 注册表")
        # tools/__init__ 公开导出与实现是同一对象
        self.assertIs(exported_list, st.list_office_files)
        self.assertIs(exported_read, st.read_office_file)
        self.assertIs(exported_write, st.write_office_file)
        self.assertIs(exported_shell, st.execute_office_shell)
        # plugin_loader 嵌套调用复用同一 shell 工具
        self.assertIs(pl.execute_office_shell, st.execute_office_shell)
        # 默认策略放行四个工具
        for name in self.OFFICE_TOOLS:
            self.assertIn(name, DEFAULT_POLICY["default_allowed_tools"])

    def test_golden_parameter_schemas(self):
        """参数名、必填性和默认值冻结（Phase 1 不得改 schema）。"""
        cases = {
            "list_office_files": {"sub_dir": ""},
            "read_office_file": {"filepath": None},                      # None = 必填
            "write_office_file": {"filepath": None, "content": None, "mode": "w"},
            "execute_office_shell": {"command": None},
        }
        for name, defaults in cases.items():
            fields = self._tool(name).args_schema.model_fields
            self.assertEqual(set(fields), set(defaults), name)
            for fname, fdefault in defaults.items():
                field = fields[fname]
                if fdefault is None:
                    self.assertTrue(field.is_required(), f"{name}.{fname} 应保持必填")
                else:
                    self.assertEqual(field.default, fdefault, f"{name}.{fname} 默认值漂移")

    # ── 行为契约 ──
    def test_golden_write_read_append_roundtrip(self):
        w = self._tool("write_office_file").invoke
        r = self._tool("read_office_file").invoke
        w({"filepath": "g/a.txt", "content": "line1"})
        w({"filepath": "g/a.txt", "content": "line2", "mode": "a"})
        content = r({"filepath": "g/a.txt"})
        self.assertIn("line1", content)
        self.assertIn("\nline2", content)  # 追加自动补换行
        w({"filepath": "g/a.txt", "content": "reset"})
        self.assertEqual(r({"filepath": "g/a.txt"}).strip(), "reset")

    def test_golden_read_truncates_beyond_10000_chars(self):
        w = self._tool("write_office_file").invoke
        r = self._tool("read_office_file").invoke
        w({"filepath": "big.log", "content": "x" * 12000})
        out = r({"filepath": "big.log"})
        self.assertLess(len(out), 10500)
        self.assertIn("[内容过长", out)

    def test_golden_list_includes_dir_and_file_markers(self):
        w = self._tool("write_office_file").invoke
        ls = self._tool("list_office_files").invoke
        w({"filepath": "doc.md", "content": "x"})
        (Path(self.office) / "sub").mkdir()
        out = ls({})
        self.assertIn("📄 doc.md", out)
        self.assertIn("📁 sub", out)

    def test_golden_error_categories(self):
        w = self._tool("write_office_file").invoke
        r = self._tool("read_office_file").invoke
        ls = self._tool("list_office_files").invoke
        self.assertIn("越权拦截", w({"filepath": "../evil.txt", "content": "x"}))
        self.assertIn("越权拦截", r({"filepath": "../../etc/passwd"}))
        self.assertIn("文件不存在", r({"filepath": "nope.txt"}))
        self.assertIn("目录不存在", ls({"sub_dir": "nope"}))
        self.assertIn("mode 参数必须是",
                      w({"filepath": "f.txt", "content": "x", "mode": "x"}))

    def test_golden_shell_whitelist_and_rejections(self):
        sh = self._tool("execute_office_shell").invoke
        w = self._tool("write_office_file").invoke
        w({"filepath": "m/a.py", "content": "print(1)"})
        # 白名单内命令各自语义保留
        self.assertIn("office 工位", sh({"command": "pwd"}))
        self.assertIn("hello", sh({"command": "echo hello"}))
        self.assertIn("a.py", sh({"command": "ls m"}))
        self.assertIn("print(1)", sh({"command": "cat m/a.py"}))
        mkdir_out = sh({"command": "mkdir g2"})
        self.assertIn("g2", mkdir_out)
        # 拒绝边界
        for bad in ("echo a && echo b", "cat a | grep b", "echo %APPDATA%",
                    "echo $HOME", "curl http://x", "rm -rf /", "python -c x"):
            self.assertIn("权限拒绝", sh({"command": bad}), msg=bad)
        self.assertIn("命令为空", sh({"command": "   "}))


if __name__ == "__main__":
    unittest.main()
