"""用户画像与五层记忆的桥接（决策 4 融合）。

NovaMind 原有 save_user_profile 工具写文件 + build_system_prompt 读文件。
改造后：画像作为 PROCEDURAL 类型高重要性记忆写入五层记忆，检索时按类型召回。
"""

from __future__ import annotations

from .schema import MemoryType
from .types import MemoryQuery


def save_persona_to_memory(manager, content: str, *, source: str | None = None):
    """把用户画像写入 procedural 记忆（高重要性，几乎不衰减）。"""
    return manager.encode(
        content,
        MemoryType.PROCEDURAL,
        importance=1.0,
        source=source or "user_profile",
        metadata={"persona": True},
    )


def load_persona_from_memory(retriever, top_k: int = 5) -> list[str]:
    """从记忆检索 procedural 类型画像，返回内容列表。"""
    results = retriever.retrieve(
        MemoryQuery(
            text="user preferences persona profile",
            type_filter=MemoryType.PROCEDURAL,
            top_k=top_k,
        )
    )
    return [r.trace.content for r in results]
