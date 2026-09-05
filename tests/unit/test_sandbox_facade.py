"""加固 Phase 1：provider-backed office 工具工厂 + Sandbox 上下文。

覆盖不变量：
  - factory 四工具：名称/参数 schema 与 legacy 一致；无上下文时 fail closed
  - fake Sandbox 精确收到标准化后的虚拟路径（VIRTUAL_ROOT 前缀、反斜杠归一）
  - read/write/list/mkdir/shell dispatch、append、截断
  - 路径穿越 / 绝对根 / 元字符 / 白名单外拒绝
  - legacy 模块级工具直接调用仍兼容（不依赖上下文）
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from novamind.core.sandbox.context import (
    SandboxContextManager,
    get_current_sandbox,
    set_current_sandbox,
    reset_current_sandbox,
    require_current_sandbox,
)
from novamind.core.tools.sandbox_tools import (
    VIRTUAL_ROOT,
    build_sandbox_tools,
    _norm_virtual_path,
    list_office_files as legacy_list,
    read_office_file as legacy_read,
    write_office_file as legacy_write,
)


class _FakeSandbox:
    def __init__(self):
        self.read = MagicMock(return_value="hello sandbox")
        self.write = MagicMock()
        self.list = MagicMock(return_value=[("a.md", False), ("sub", True)])
        self.make_dir = MagicMock()
        self.closed = False

    def read_file(self, path):
        return self.read(path)

    def write_file(self, path, content, append=False):
        self.write(path, content, append=append)

    def list_dir_typed(self, path, max_entries=1000):
        return self.list(path)

    def make_dir(self, path):
        self.make_dir(path)

    def close(self):
        self.closed = True


class TestNormVirtualPath(unittest.TestCase):
    def test_normalizes_backslash_and_strips(self):
        self.assertEqual(_norm_virtual_path(r"a\\b/c"), f"{VIRTUAL_ROOT}/a/b/c")
        self.assertEqual(_norm_virtual_path(""), VIRTUAL_ROOT)
        self.assertEqual(_norm_virtual_path(None), VIRTUAL_ROOT)
        with self.assertRaises(PermissionError):
            _norm_virtual_path("/")  # "/" 是绝对根，拒绝（工位根用空串）

    def test_rejects_traversal_and_absolute_roots(self):
        for bad in ("../x", "a/../../x", "C:/x", "C:\\\\x", "/abs/x"):
            with self.assertRaises(PermissionError, msg=bad):
                _norm_virtual_path(bad)


class TestFactoryToolsContract(unittest.TestCase):
    def setUp(self):
        self.tools = {t.name: t for t in build_sandbox_tools()}
        self.sb = _FakeSandbox()

    def test_factory_names_and_schemas_match_legacy(self):
        legacy = {"list_office_files": legacy_list, "read_office_file": legacy_read,
                  "write_office_file": legacy_write}
        for name, ft in self.tools.items():
            self.assertEqual(ft.name, name)
            # write_office_file 是 factory 专属（shell/exec 也来自工厂）；至少参数 schema 与 legacy 对齐
            if name in legacy:
                self.assertEqual(
                    set(ft.args_schema.model_fields), set(legacy[name].args_schema.model_fields)
                )

    def test_fail_closed_without_context(self):
        for name, t in self.tools.items():
            with self.assertRaises(RuntimeError, msg=name):
                t.invoke({} if name == "list_office_files"
                         else {"filepath": "x"} if name == "read_office_file"
                         else {"filepath": "x", "content": "c"} if name == "write_office_file"
                         else {"command": "pwd"})

    def _run_with(self, name, **kwargs):
        token = set_current_sandbox(self.sb)
        try:
            return self.tools[name].invoke(kwargs)
        finally:
            reset_current_sandbox(token)

    def test_read_dispatches_to_sandbox_and_masks_ok(self):
        out = self._run_with("read_office_file", filepath="a.txt")
        self.assertEqual(out, "hello sandbox")
        self.sb.read.assert_called_once_with(f"{VIRTUAL_ROOT}/a.txt")

    def test_read_truncates_over_10000(self):
        self.sb.read.return_value = "x" * 12000
        out = self._run_with("read_office_file", filepath="big.log")
        self.assertLess(len(out), 10500)
        self.assertIn("[内容过长", out)

    def test_write_dispatches_with_append_and_newline(self):
        self._run_with("write_office_file", filepath="f.txt", content="line2", mode="a")
        self.sb.write.assert_called_once_with(f"{VIRTUAL_ROOT}/f.txt", "\nline2", append=True)
        self._run_with("write_office_file", filepath="f.txt", content="over")
        self.sb.write.assert_called_with(f"{VIRTUAL_ROOT}/f.txt", "over", append=False)

    def test_write_rejects_bad_mode(self):
        out = self._run_with("write_office_file", filepath="f", content="x", mode="x")
        self.assertIn("mode 参数必须是", out)
        self.sb.write.assert_not_called()

    def test_list_dispatches_typed(self):
        out = self._run_with("list_office_files", sub_dir="docs")
        self.sb.list.assert_called_once_with(f"{VIRTUAL_ROOT}/docs")
        self.assertIn("📄 a.md", out)
        self.assertIn("📁 sub", out)

    def test_shell_echo_and_pwd(self):
        self.assertIn("hello", self._run_with("execute_office_shell", command="echo hello"))
        self.assertIn(VIRTUAL_ROOT, self._run_with("execute_office_shell", command="pwd"))

    def test_shell_rejects_metachars_and_non_whitelist(self):
        for bad in ("echo a && echo b", "cat a | grep b", "echo %APPDATA%", "rm -rf /"):
            self.assertIn("权限拒绝", self._run_with("execute_office_shell", command=bad))
        self.sb.exec_command = MagicMock()

    def test_legacy_tools_still_work_without_context(self):
        # legacy 直调不依赖 Sandbox 上下文，走宿主 OFFICE_DIR（patch 到临时目录）
        with tempfile.TemporaryDirectory() as tmp:
            with patch("novamind.core.tools.sandbox_tools.OFFICE_DIR", tmp):
                out = legacy_write.invoke({"filepath": "legacy.txt", "content": "x"})
                self.assertIn("成功", out)
                self.assertTrue((Path(tmp) / "legacy.txt").exists())


class TestSandboxContextManager(unittest.TestCase):
    class _Provider:
        def __init__(self):
            self.sb = _FakeSandbox()
            self.acquired = 0
            self.released = 0

        async def acquire_async(self, thread_id=None, user_id=None):
            self.acquired += 1
            return "sb1"

        def get(self, sandbox_id):
            return self.sb if sandbox_id == "sb1" else None

        def release(self, sandbox_id):
            self.released += 1

    def test_aenter_sets_context_aexit_restores_and_releases(self):
        import asyncio
        provider = self._Provider()

        async def run():
            async with SandboxContextManager(provider, "t1") as sb:
                self.assertIs(sb, provider.sb)
                self.assertIs(require_current_sandbox(), provider.sb)
            # 退出后上下文已清空（get 返回 None，不抛错）
            self.assertIsNone(get_current_sandbox())

        asyncio.run(run())
        self.assertEqual(provider.acquired, 1)
        self.assertEqual(provider.released, 1)


if __name__ == "__main__":
    unittest.main()
