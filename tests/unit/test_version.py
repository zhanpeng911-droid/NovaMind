"""Version identity stays consistent without loading application dependencies."""
import builtins
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tomllib

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_IMPORT_VERSION = """
import builtins
import importlib.metadata
import json
import sys

sys.path.insert(0, sys.argv[1])
sys.frozen = sys.argv[3] == "packaged"
def installed_version(name):
    assert name == "novamind"
    if sys.argv[2] == "missing":
        raise importlib.metadata.PackageNotFoundError(name)
    return sys.argv[2]
importlib.metadata.version = installed_version
original_import = builtins.__import__
def isolated_import(name, *args, **kwargs):
    if name == "dotenv" or name.startswith(("novamind.core", "novamind.webui")):
        raise AssertionError("Package version imported application code: " + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = isolated_import
import novamind
print(json.dumps({"version": novamind.__version__}))
"""


def _package_version(project_root, *, installed="9.8.7", packaged=False):
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _IMPORT_VERSION, str(project_root), installed,
         "packaged" if packaged else "source"],
        check=True, capture_output=True, text=True, cwd=project_root,
    )
    return json.loads(completed.stdout)["version"]


@pytest.fixture
def isolated_package(tmp_path):
    package = tmp_path / "novamind"
    package.mkdir()
    for name in ("__init__.py", "_version.py"):
        source = _ROOT / "novamind" / name
        if source.exists():
            shutil.copyfile(source, package / name)
    return tmp_path


def test_public_version_matches_source_project_without_application_imports():
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert _package_version(_ROOT) == project["project"]["version"]


def test_source_project_takes_precedence_over_installed_metadata(isolated_package):
    (isolated_package / "pyproject.toml").write_text('[project]\nversion = "4.5.6"\n')
    assert _package_version(isolated_package) == "4.5.6"


@pytest.mark.parametrize("manifest", [None, "invalid toml =", "[project]", "[project]\nversion = 2"])
def test_installed_version_is_used_without_valid_source(isolated_package, manifest):
    if manifest is not None:
        (isolated_package / "pyproject.toml").write_text(manifest)
    assert _package_version(isolated_package, installed="4.5.7") == "4.5.7"


def test_missing_version_evidence_is_explicit(isolated_package):
    assert _package_version(isolated_package, installed="missing") == "unknown"


def test_frozen_public_version_uses_embedded_build_only(isolated_package):
    (isolated_package / "pyproject.toml").write_text('[project]\nversion = "8.8.8"\n')
    manifest = isolated_package / "novamind" / "webui" / "_build_info.json"
    manifest.parent.mkdir()
    manifest.write_text('{"version": "3.4.5", "build_time": "2026-10-23T06:30:00+00:00"}')
    assert _package_version(isolated_package, packaged=True) == "3.4.5"


@pytest.mark.parametrize("manifest", [None, "bad json", "[]", '{"version": " "}'])
def test_missing_frozen_version_never_falls_back_to_source_or_install(
    isolated_package, manifest,
):
    (isolated_package / "pyproject.toml").write_text('[project]\nversion = "8.8.8"\n')
    if manifest is not None:
        target = isolated_package / "novamind" / "webui" / "_build_info.json"
        target.parent.mkdir()
        target.write_text(manifest)
    assert _package_version(isolated_package, packaged=True) == "unknown"


def test_project_version_reader_rejects_missing_or_invalid_evidence(tmp_path):
    """Build stamping must fail instead of copying an unrelated installed version."""
    script = _ROOT / "packaging" / "build_metadata.py"
    builder = runpy.run_path(str(script))["write_build_info"]
    with pytest.raises((OSError, ValueError, KeyError, TypeError)):
        builder(tmp_path)
    assert not (tmp_path / "build" / "metadata" / "_build_info.json").exists()


def test_build_stamping_does_not_import_application(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "3.4.5"\n')
    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name == "novamind" or name.startswith("novamind."):
            raise AssertionError(f"Build metadata imported application code: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", isolated_import)
    builder = runpy.run_path(str(_ROOT / "packaging" / "build_metadata.py"))["write_build_info"]
    data = json.loads(builder(tmp_path).read_text(encoding="utf-8"))
    assert data["version"] == "3.4.5"
