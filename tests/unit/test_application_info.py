"""Build identity checks; no GUI, user files or external services."""
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
import json
from pathlib import Path
import runpy

import pytest

from novamind.core import config
from novamind import _version
from novamind.webui import application_info


@pytest.fixture
def identity_paths(tmp_path, monkeypatch):
    manifest = tmp_path / "_build_info.json"
    project = tmp_path / "pyproject.toml"
    monkeypatch.setattr(_version, "_BUILD_INFO_PATH", manifest)
    monkeypatch.setattr(_version, "_PROJECT_FILE", project)
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path / "学习 & 工作"))
    return manifest, project


def test_packaged_identity_uses_embedded_build_not_current_project(identity_paths, monkeypatch):
    manifest, project = identity_paths
    manifest.write_text(json.dumps({
        "version": "3.0.0", "build_time": "2026-10-23T14:30:00+08:00",
    }), encoding="utf-8")
    project.write_text('[project]\nversion = "4.0.0"\n', encoding="utf-8")
    monkeypatch.setattr(application_info.sys, "frozen", True, raising=False)
    info = application_info.get_application_info()
    assert info == {
        "version": "3.0.0", "build_time": "2026-10-23T06:30:00+00:00",
        "mode": "packaged", "workspace": str(Path(config.WORKSPACE_DIR).resolve()),
    }
    assert application_info.get_application_info() == info  # not a fresh startup timestamp


def test_source_does_not_reuse_a_stale_build_record(identity_paths, monkeypatch):
    manifest, project = identity_paths
    manifest.write_text('{"version":"old", "build_time":"2020-01-01T00:00:00+00:00"}')
    project.write_text('[project]\nversion = "3.0.0"\n', encoding="utf-8")
    monkeypatch.setattr(application_info.sys, "frozen", False, raising=False)
    info = application_info.get_application_info()
    assert info["version"] == "3.0.0"
    assert info["build_time"] is None
    assert info["mode"] == "source"


@pytest.mark.parametrize("content", [None, "invalid json", "[]", '{"version": null}'])
def test_unavailable_packaged_metadata_is_not_fabricated(identity_paths, monkeypatch, content):
    manifest, _ = identity_paths
    if content is not None:
        manifest.write_text(content)
    monkeypatch.setattr(application_info.sys, "frozen", True, raising=False)
    info = application_info.get_application_info()
    assert info["version"] == "unknown"
    assert info["build_time"] is None
    assert info["mode"] == "packaged"
    assert info["workspace"]


@pytest.mark.parametrize("built_at", [None, 123, "bad date", "2026-10-23T14:30:00"])
def test_invalid_or_unzoned_build_time_is_not_displayed(identity_paths, monkeypatch, built_at):
    manifest, _ = identity_paths
    manifest.write_text(json.dumps({"version": "3.0.0", "build_time": built_at}))
    monkeypatch.setattr(application_info.sys, "frozen", True, raising=False)
    info = application_info.get_application_info()
    assert info["version"] == "3.0.0"
    assert info["build_time"] is None


def test_installed_package_version_fallback(identity_paths, monkeypatch):
    monkeypatch.setattr(application_info.sys, "frozen", False, raising=False)
    monkeypatch.setattr(_version, "version", lambda name: "3.1.0")
    assert application_info.get_application_info()["version"] == "3.1.0"


def test_missing_source_metadata_is_explicit(identity_paths, monkeypatch):
    monkeypatch.setattr(application_info.sys, "frozen", False, raising=False)

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(_version, "version", missing)
    assert application_info.get_application_info()["version"] == "unknown"


def test_spec_builder_stamps_project_version_and_current_utc_time(tmp_path):
    tmp_path.joinpath("pyproject.toml").write_text('[project]\nversion = "3.4.5"\n')
    script = Path(__file__).resolve().parents[2] / "packaging" / "build_metadata.py"
    builder = runpy.run_path(str(script))["write_build_info"]
    before = datetime.now(timezone.utc).replace(microsecond=0)
    target = builder(tmp_path)
    after = datetime.now(timezone.utc)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["version"] == "3.4.5"
    assert before <= datetime.fromisoformat(data["build_time"]) <= after
    assert datetime.fromisoformat(data["build_time"]).utcoffset().total_seconds() == 0
    assert target.parent.is_relative_to(tmp_path / "build")
    assert not (tmp_path / "novamind").exists()  # source checkout is not stamped
