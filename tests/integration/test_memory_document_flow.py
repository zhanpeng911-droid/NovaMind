"""Editable memory: real SQLite/file conflicts, isolated from personal data."""
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from novamind.core import config, personal
from novamind.core.memory import document, governance_runtime
from novamind.core.memory.governance import MemoryGovernanceService, MemoryGovernanceStore, MemoryState
from novamind.core.memory.strategies.default.store import MarkdownFileStore
from novamind.webui.server import app


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MEMORY_DIR", str(tmp_path / "documents"))
    store = MemoryGovernanceStore(str(tmp_path / "governance.sqlite3"))
    service = MemoryGovernanceService(store, trace_store=MarkdownFileStore(str(tmp_path / "traces")))
    monkeypatch.setattr(governance_runtime, "get_governance_service", lambda: service)
    token = personal.request_context.set({"memory_mode": "normal", "scope": "global"})
    try:
        yield TestClient(app), service
    finally:
        personal.request_context.reset(token)
        store.close()


def write(client, current, text):
    return client.put("/personal/memory-document", json={
        "content": text, "revision": current["revision"], "scope": current["scope"],
    })


def test_text_roundtrip_reuses_records_and_reenables_explicitly_removed_item(memory):
    client, service = memory
    first = client.get("/personal/memory-document").json()
    text = "- [偏好] 回答先给结论\n\n- [项目] 使用 pytest 做验证"
    result = write(client, first, text)
    assert result.status_code == 200
    records = service.list_governed()
    ids = {record.memory_id for record in records}
    assert len(ids) == 2
    assert write(client, result.json(), result.json()["content"]).status_code == 200
    assert {record.memory_id for record in service.list_governed()} == ids
    reduced = write(client, result.json(), "- [偏好] 回答先给结论").json()
    assert any(record.state == MemoryState.DISABLED for record in service.list_governed())
    assert write(client, reduced, text).status_code == 200
    assert {record.memory_id for record in service.list_governed()} == ids
    assert all(record.state == MemoryState.ACTIVE for record in service.list_governed())


def test_file_and_governance_conflict_returns_both_and_rejects_stale_save(memory):
    client, service = memory
    original = client.get("/personal/memory-document").json()
    Path(original["path"]).write_text("- [项目] 文件中的手工修改", encoding="utf-8")
    service.save_explicit("治理库中的并行修改", category="experience")
    conflict = client.get("/personal/memory-document").json()
    assert conflict["conflict"]
    assert "文件中的手工修改" in conflict["content"]
    assert "治理库中的并行修改" in conflict["server_content"]
    assert write(client, original, "未合并的旧草稿").status_code == 409
    assert client.get("/personal/memory-document").json()["conflict"]
    merged = conflict["content"] + "\n\n" + conflict["server_content"].split("\n\n", 1)[1]
    response = write(client, conflict, merged)
    assert response.status_code == 200
    assert "文件中的手工修改" in response.json()["content"]
    assert "治理库中的并行修改" in response.json()["content"]


def test_direct_file_edit_is_reconciled_but_tombstones_cannot_be_restored(memory):
    client, service = memory
    first = client.get("/personal/memory-document").json()
    Path(first["path"]).write_text("- [项目] 用户喜欢绿茶", encoding="utf-8")
    current = client.get("/personal/memory-document").json()
    record = next(record for record in service.list_governed() if record.state == MemoryState.ACTIVE)
    service.delete(record.memory_id)
    after_delete = client.get("/personal/memory-document").json()
    assert "用户喜欢绿茶" not in after_delete["content"]
    assert write(client, after_delete, current["content"]).status_code == 422
    assert service.store.is_tombstoned(record.memory_id)
    assert service.store.get(record.memory_id).state == MemoryState.DELETED


def test_project_scope_and_unconfirmed_records_do_not_leak_into_global_document(memory):
    client, service = memory
    service.save_explicit("项目 A 使用 Python", category="project_context", scope="a")
    service.save_explicit("未确认的项目候选", confirmed=False, scope="a")
    global_doc = client.get("/personal/memory-document").json()
    scoped = client.get("/personal/memory-document?scope=a").json()
    assert "项目 A" not in global_doc["content"]
    assert "项目 A" in scoped["content"]
    assert "未确认" not in scoped["content"]
    assert Path(global_doc["path"]) != Path(scoped["path"])
    assert Path(scoped["path"]).parent == Path(config.MEMORY_DIR)


def test_invalid_document_is_rejected_before_partial_changes(memory):
    client, service = memory
    current = client.get("/personal/memory-document").json()
    for content in ("有效项目\n\n密码不应进入记忆", "- [项目] " + "长" * 4001):
        assert write(client, current, content).status_code == 422
        assert service.list_governed() == []
    assert client.get("/personal/memory-document?scope=").status_code == 422


def test_scope_context_reflects_direct_file_edits_but_no_record_mode_does_not_write(memory):
    client, service = memory
    global_doc = client.get("/personal/memory-document").json()
    Path(global_doc["path"]).write_text("- [偏好] 先给简短结论", encoding="utf-8")
    service.save_explicit("只在项目 A 使用", category="project_context", scope="a")
    result = document.context_records(service, "a")
    assert {entry.trace.content for entry in result} == {"先给简短结论", "只在项目 A 使用"}
    Path(global_doc["path"]).write_text("- [偏好] 尚未同步的文件", encoding="utf-8")
    token = personal.request_context.set({"memory_mode": "no_record"})
    try:
        result = document.context_records(service, "a")
        assert "尚未同步的文件" not in {entry.trace.content for entry in result}
    finally:
        personal.request_context.reset(token)
