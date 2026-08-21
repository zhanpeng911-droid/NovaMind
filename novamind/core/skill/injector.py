"""Skill 注入文本构建 — markdown block，从 SKILL.md 文件读 body（去 frontmatter）。"""

from __future__ import annotations

from pathlib import Path

from .types import SkillRecord


def build_injection_text(skills: list[SkillRecord]) -> str:
    """构建 active skills 的 markdown 注入块。无 skill 返空串。"""
    if not skills:
        return ""
    lines: list[str] = ["# Active Skills", ""]
    for rec in skills:
        body = _read_body(rec.path)
        lines.append(f"### Skill: {rec.name}")
        lines.append(f"**Path**: {rec.path}")
        lines.append("")
        lines.append(body.strip())
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def _read_body(path: str) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\r\n")
    return content
