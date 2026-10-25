"""Stamp each desktop build from pyproject.toml, without importing the app."""
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy

_read_project_version = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "novamind" / "_version.py"),
)["read_project_version"]


def write_build_info(project_root: Path) -> Path:
    app_version = _read_project_version(project_root / "pyproject.toml")
    target = project_root / "build" / "metadata" / "_build_info.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": app_version,
        "build_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2) + "\n", encoding="utf-8")
    return target
