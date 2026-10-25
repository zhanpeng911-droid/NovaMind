"""Single workspace settings and per-request personal context.

No model calls and no desktop imports: CLI, Web and desktop share this source.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import config

request_context: ContextVar[Mapping[str, Any]] = ContextVar("personal_request", default=MappingProxyType({}))
_lock = threading.RLock()
desktop_status: dict[str, Any] = {"hotkey_error": "", "notification": "程序运行期间可提醒"}


def report_turn(state: str, message: str = "") -> None:
    desktop_status["last_turn"] = {"id": uuid.uuid4().hex, "state": state, "message": message}


DEFAULTS = {
    "learning": {"enabled": False, "min_samples": 3, "retention_days": 30,
                 "max_observations": 1000, "cooldown_days": 7},
    "appearance": {"theme": "system"},
    "memory": {"recall_enabled": True, "auto_extract": True, "token_budget": 2000},
    "style": {"preset": "default", "custom": ""},
    "desktop": {"bubble_visible": True, "startup_bubble": True, "autostart": False, "open_workspace": True, "size": 48,
                "opacity": 1.0, "theme": "follow", "accent": "blue", "icon": "brand",
                "snap": True, "motion": True, "status": True, "icon_fit": "contain",
                "hotkey": "Ctrl+Shift+Space", "bubble_x": None, "bubble_y": None},
}


def settings() -> dict[str, Any]:
    with _lock:
        result = copy.deepcopy(DEFAULTS)
        path = Path(config.WORKSPACE_DIR) / "personal_settings.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            for section in result:
                result[section].update(saved.get(section, {}))
        return result


def update_settings(section: str, changes: dict[str, Any]) -> dict[str, Any]:
    if section not in DEFAULTS:
        raise ValueError("Unknown settings section")
    with _lock:
        current = settings()
        current[section].update(changes)
        path = Path(config.WORKSPACE_DIR) / "personal_settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return current


def memory_allowed(write: bool = False) -> bool:
    ctx = request_context.get()
    if ctx.get("memory_mode") == "private":
        return False
    if write and ctx.get("memory_mode") == "no_record":
        return False
    return write or settings()["memory"]["recall_enabled"]


def style_instruction() -> str:
    ctx = request_context.get()
    saved = settings()["style"]
    preset = ctx.get("style") or saved["preset"]
    styles = {"default": "", "concise": "回答简洁直接，优先给结论。",
              "detailed": "详细解释思路，必要时给例子。", "warm": "语气温和自然，避免过度客套。",
              "formal": "使用清晰、专业、正式的表达。", "custom": saved["custom"]}
    text = styles.get(preset, "")
    return ("\n【表达偏好】仅调节表达，不改变事实、权限及用户当前要求：\n" + text) if text else ""
