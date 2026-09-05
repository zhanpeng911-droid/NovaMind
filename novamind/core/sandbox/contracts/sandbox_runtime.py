"""中立沙箱运行时契约（三组件之一）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types import GrepMatch


@runtime_checkable
class SandboxRuntime(Protocol):
    """中立沙箱运行时协议（裸执行契约）。

    只负责裸执行（exec/read/write），不知路径翻译、不做安全检查。
    Local / Docker 各写 adapter。异构差异封装在 adapter 内。

    所有方法遵守调用方传入的路径契约（Local 路径已翻译为物理路径；Docker 直传虚拟路径）。
    异常类型：全抛 SandboxError 子类，runtime 实现负责包装内置异常。
    """

    def exec_command(self, command: str) -> str: ...

    def read_file(self, path: str) -> str: ...

    def write_file(self, path: str, content: str, append: bool = False) -> None: ...

    def list_dir(self, path: str, max_depth: int = 2, max_entries: int = 1000) -> list[str]: ...

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]: ...

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]: ...

    def download_file(self, path: str) -> bytes: ...

    def make_dir(self, path: str) -> None: ...

    def list_dir_typed(self, path: str, max_entries: int = 1000) -> list[tuple[str, bool]]:
        """单层目录列表，返回 (名称, 是否目录)。"""
        ...

    def update_file(self, path: str, content: bytes) -> None: ...

    def close(self) -> None: ...
