"""Application identity from source, installed metadata, or an embedded build.

Keep this module standard-library-only: importing the public package must not
load configuration, create a workspace, or import the WebUI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys
import tomllib

_PROJECT_FILE = Path(__file__).resolve().parents[1] / "pyproject.toml"
_BUILD_INFO_PATH = Path(__file__).resolve().parent / "webui" / "_build_info.json"


def read_project_version(project_file: Path) -> str:
    """Read the authoritative source version; invalid build inputs must fail."""
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    value = project["project"]["version"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("project.version must be a nonempty string")
    return value


def _source_version() -> str:
    try:
        return read_project_version(_PROJECT_FILE)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        value = version("novamind")
        return value if isinstance(value, str) and value.strip() else "unknown"
    except PackageNotFoundError:
        return "unknown"


def _packaged_info() -> tuple[str, str | None]:
    try:
        data = json.loads(_BUILD_INFO_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown", None
    if not isinstance(data, dict):
        return "unknown", None
    value = data.get("version")
    app_version = value if isinstance(value, str) and value.strip() else "unknown"
    try:
        built_at = datetime.fromisoformat(data["build_time"])
        # An unzoned timestamp cannot reliably identify a build across machines.
        if built_at.tzinfo is None:
            return app_version, None
        return app_version, built_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (KeyError, TypeError, ValueError, OverflowError):
        return app_version, None


def get_version_info() -> tuple[str, str | None]:
    """Return version evidence and its build time, when running a frozen build."""
    if getattr(sys, "frozen", False):
        return _packaged_info()
    return _source_version(), None


def get_version() -> str:
    return get_version_info()[0]
