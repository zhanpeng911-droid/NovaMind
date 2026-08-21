"""安全检查契约（三组件之一）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecurityGuard(Protocol):
    """安全检查契约（白名单 + 路径穿越拒绝 + 危险命令拦截）。

    Local 严格白名单；Docker 容器内已隔离，宽松检查。
    不做 mask_output（脱敏归 PathTranslator，Guard 专注 validate）。

    INVARIANT:
    - validate_path 抛 SandboxPermissionError（路径穿越 / 越界）
    - validate_command 抛 SandboxPermissionError（命令含越界路径 / 危险模式）
    """

    def validate_path(self, path: str, *, write: bool = False) -> None:
        """验证路径在白名单内 + 拒路径穿越。write=True 检查写权限。"""
        ...

    def validate_command(self, command: str) -> None:
        """验证 bash 命令里的路径在白名单内 + 拒危险命令。"""
        ...
