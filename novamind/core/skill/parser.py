"""SKILL.md YAML frontmatter 解析 + .skill_id sidecar。

吸收 Poirot `skill/parser.py`：
- .skill_id sidecar 持久 id；BUILTIN 用确定性 id {name}__builtin
- frontmatter 必需 name + description
- content_hash = sha256(SKILL.md 全文)[:16]
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

import yaml

from .types import SkillLineage, SkillRecord

_SKILL_ID_FILE = ".skill_id"
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)", re.DOTALL)


def _generate_skill_id(name: str, origin: str, generation: int) -> str:
    if origin == "BUILTIN":
        return f"{name}__builtin"
    short = uuid.uuid4().hex[:8]
    if origin == "IMPORTED":
        return f"{name}__imp_{short}"
    return f"{name}__v{generation}_{short}"


def read_or_create_skill_id(skill_dir: Path, name: str, origin: str = "IMPORTED", generation: int = 0) -> str:
    if origin == "BUILTIN":
        return _generate_skill_id(name, origin, generation)
    sidecar = skill_dir / _SKILL_ID_FILE
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip()
    skill_id = _generate_skill_id(name, origin, generation)
    sidecar.write_text(skill_id, encoding="utf-8")
    return skill_id


def parse_skill_file(skill_file: Path, origin: str = "IMPORTED") -> SkillRecord:
    """解析 SKILL.md → SkillRecord。"""
    content = skill_file.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ValueError(f"SKILL.md {skill_file} missing YAML frontmatter")
    fm_raw, _body = match.group(1), match.group(2)
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md {skill_file} frontmatter YAML parse error: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError(f"SKILL.md {skill_file} frontmatter must be a mapping")

    name = fm.get("name")
    description = fm.get("description")
    if not name:
        raise ValueError(f"SKILL.md {skill_file} missing 'name'")
    if not description:
        raise ValueError(f"SKILL.md {skill_file} missing 'description'")

    allowed_tools_raw = fm.get("allowed-tools") or []
    allowed_tools = tuple(allowed_tools_raw) if allowed_tools_raw else ()
    enabled = bool(fm.get("enabled", True))

    skill_dir = skill_file.parent
    skill_id = read_or_create_skill_id(skill_dir, name, origin=origin)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    return SkillRecord(
        skill_id=skill_id,
        name=name,
        path=str(skill_file),
        content_hash=content_hash,
        description=description,
        allowed_tools=allowed_tools,
        enabled=enabled,
        lineage=SkillLineage(origin=origin),
    )
