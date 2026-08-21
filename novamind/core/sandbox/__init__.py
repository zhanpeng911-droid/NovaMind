"""三组件沙箱（方案 C）：Runtime + PathTranslator + SecurityGuard。

吸收 Poirot 沙箱架构：Sandbox 编排类组合三组件，切沙箱只换组件不改编排。
Local 模式完整可用；Docker 模式骨架（真实运行需 Docker 环境）。
"""

from .sandbox import Sandbox
from .exceptions import (
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxCommandError,
    SandboxFileError,
    SandboxPermissionError,
    SandboxFileNotFoundError,
)
from .types import PathMapping, SandboxInfo, GrepMatch
from .local import LocalSandboxProvider, default_office_path_mappings
from .docker import DockerSandboxProvider

__all__ = [
    "Sandbox",
    "SandboxError",
    "SandboxNotFoundError",
    "SandboxRuntimeError",
    "SandboxCommandError",
    "SandboxFileError",
    "SandboxPermissionError",
    "SandboxFileNotFoundError",
    "PathMapping",
    "SandboxInfo",
    "GrepMatch",
    "LocalSandboxProvider",
    "DockerSandboxProvider",
    "default_office_path_mappings",
]
