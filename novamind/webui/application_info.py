"""Read-only application identity for the existing diagnostic page."""
from __future__ import annotations

from pathlib import Path
import sys

from novamind._version import get_version_info


def get_application_info() -> dict[str, str | None]:
    from novamind.core import config

    packaged = bool(getattr(sys, "frozen", False))
    app_version, built_at = get_version_info()
    return {
        "version": app_version,
        "build_time": built_at,
        "mode": "packaged" if packaged else "source",
        "workspace": str(Path(config.WORKSPACE_DIR).resolve()),
    }
