"""One-time, non-destructive snapshot before personal-agent persistence changes."""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import config
from .personal import settings, update_settings

_lock = threading.Lock()


def ensure_snapshot() -> None:
    with _lock:
        if settings()["memory"].get("upgrade_snapshot"):
            return
        from .memory.config import get_memory_config
        workspace = Path(config.WORKSPACE_DIR)
        target = workspace / "backups" / ("personal-v1-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        target.mkdir(parents=True, exist_ok=False)
        manifest = []
        for name in ("state.sqlite3", "memory_governance.sqlite3", "skill_store.sqlite3"):
            source = workspace / name
            if source.is_file():
                with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src:
                    with sqlite3.connect(target / name) as dst:
                        src.backup(dst)
                manifest.append({"source": str(source), "backup": name, "memory_body": False})
        copies = [(workspace / name, name, False) for name in
                  ("tasks.json", "personal_settings.json", "desktop_settings.json")]
        profile = Path(config.PROFILE_PATH)
        copies.append((profile, "legacy_profile.md", True))
        for source in Path(config.PROFILE_BACKUP_DIR).glob("user_profile.*.md"):
            copies.append((source, "profile_backups/" + source.name, True))
        storage = Path(get_memory_config().storage_path)
        if not storage.is_absolute():
            storage = workspace / storage
        copies.append((storage / "traces.md", "memory_traces.md", True))
        for source, name, memory_body in copies:
            if source.is_file():
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                manifest.append({"source": str(source), "backup": name, "memory_body": memory_body})
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        update_settings("memory", {"upgrade_snapshot": str(target)})
