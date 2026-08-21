"""沙箱类型定义（吸收 Poirot `sandbox/types.py`）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


@dataclass(frozen=True)
class PathMapping:
    """虚拟路径到宿主路径映射。LocalSandbox 专属。

    frozen = 不可变，线程安全。按 container_path 长度降序匹配（最长前缀优先）。
    """

    container_path: str
    local_path: str
    read_only: bool = False


class ResolvedPath(NamedTuple):
    """路径解析结果：同时返回物理路径和匹配的映射。"""

    path: str
    mapping: PathMapping | None


@dataclass
class GrepMatch:
    """grep 工具的单条匹配结果。跨实现统一。"""

    path: str
    line_number: int
    line: str


@dataclass
class SandboxInfo:
    """跨进程可恢复的沙箱元数据。

    container_name / container_id 仅 Docker 模式有；Local 模式只有 sandbox_id。
    """

    sandbox_id: str
    sandbox_url: str = ""
    container_name: str | None = None
    container_id: str | None = None
    created_at: str = field(default_factory=lambda: "")

    def to_dict(self) -> dict:
        return {
            "sandbox_id": self.sandbox_id,
            "sandbox_url": self.sandbox_url,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SandboxInfo":
        return cls(
            sandbox_id=data["sandbox_id"],
            sandbox_url=data.get("sandbox_url", ""),
            container_name=data.get("container_name"),
            container_id=data.get("container_id"),
            created_at=data.get("created_at", ""),
        )
