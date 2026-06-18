"""
Shared task file storage helpers.
"""
from __future__ import annotations
import json
import os
import threading
from typing import Any

from .config import TASKS_FILE


TASKS_LOCK = threading.Lock()


def load_tasks_unlocked() -> list[dict[str, Any]]:
    """Load tasks while the caller holds TASKS_LOCK."""
    if not os.path.exists(TASKS_FILE):
        return []

    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return []
        tasks = json.loads(content)
        return tasks if isinstance(tasks, list) else []


def write_tasks_unlocked(tasks: list[dict[str, Any]]) -> None:
    """Atomically write tasks while the caller holds TASKS_LOCK."""
    os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
    tmp_path = f"{TASKS_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, TASKS_FILE)
