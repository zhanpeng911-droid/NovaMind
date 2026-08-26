"""SnapshotExecutor — P4 压缩前存快照。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from ._constants import CST

logger = logging.getLogger(__name__)


class SnapshotExecutor:
    def __init__(self, snapshot_dir: str = ".novamind/snapshots") -> None:
        self._dir = snapshot_dir

    def snapshot_if_pending(self, governance: dict | None, messages: list, state: Any) -> dict | None:
        governance = governance or {}
        pending = (governance.get("default") or {}).get("pending") or []
        if "P4" not in pending:
            return None
        snapshot = self._build_snapshot(messages, state)
        path = self._write_to_disk(snapshot)
        if path is None:
            return None
        g = dict(governance)
        d = dict(g.get("default") or {})
        d["snapshot_path"] = path
        metrics = dict(d.get("metrics") or {})
        metrics["snapshot_count"] = metrics.get("snapshot_count", 0) + 1
        d["metrics"] = metrics
        g["default"] = d
        return g

    def _build_snapshot(self, messages: list, state: Any) -> dict:
        state = state or {}
        return {
            "created_at": datetime.now(CST).isoformat(),
            "messages": [self._serialize_msg(m) for m in messages],
        }

    @staticmethod
    def _serialize_msg(msg: Any) -> dict:
        return {
            "type": type(msg).__name__,
            "content": msg.content if isinstance(msg.content, str) else str(msg.content),
            "id": getattr(msg, "id", None),
            "tool_calls": getattr(msg, "tool_calls", None) or None,
            "tool_call_id": getattr(msg, "tool_call_id", None),
            "name": getattr(msg, "name", None),
        }

    def _write_to_disk(self, snapshot: dict) -> str | None:
        try:
            abs_dir = os.path.abspath(self._dir)
            os.makedirs(abs_dir, exist_ok=True)
            ts = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
            filename = f"snapshot-{ts}.json"
            filepath = os.path.join(abs_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
            return filepath
        except (OSError, TypeError) as exc:
            logger.error("snapshot write failed: %s", exc)
            return None
