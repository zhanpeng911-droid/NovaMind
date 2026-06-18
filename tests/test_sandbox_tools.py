"""
NovaMind sandbox tool security tests.
"""
import os
import unittest

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
