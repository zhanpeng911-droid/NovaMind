"""Editable MEMORY.md projection over the existing governed memory store.

No second recall database: edits are reconciled to governed records. Disabled,
deleted, rejected and unconfirmed facts never enter the document or context.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .. import config
from .governance import GovernanceConflictError, GovernanceError, MemoryState

_LABELS = {"preference": "偏好", "project_context": "项目", "experience": "经验", "active_task": "任务", "task_context": "任务"}
_CATEGORIES = {"偏好": "preference", "项目": "project_context", "经验": "experience", "任务": "active_task"}
_HEADER = "# NovaMind 个人记忆\n\n"


def _paths(scope):
    suffix = "" if scope == "global" else "." + hashlib.sha256(scope.encode()).hexdigest()[:16]
    root = Path(config.MEMORY_DIR)
    return root / ("MEMORY" + suffix + ".md"), root / (".MEMORY" + suffix + ".json")


def _records(service, scope):
    return [r for r in service.list_governed(states=[MemoryState.ACTIVE], scope=scope)
            if r.user_confirmed and not r.superseded_by and not service.store.is_tombstoned(r.memory_id)
            and (trace := service.trace_store.get(r.memory_id)) and not trace.metadata.get("forgotten")]


def _text(service, records):
    return _HEADER + "\n\n".join("- [" + _LABELS.get(r.category, "经验") + "] "
                                  + service.content_for(r.memory_id).replace("\n", "\n  ") for r in records) + "\n"


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _signature(records):
    return _digest(json.dumps([(r.memory_id, r.revision) for r in records]))


def _parse(text):
    if len(text) > 32000:
        raise GovernanceError("记忆文本最多 32000 字符")
    text = text.strip()
    if text.startswith(_HEADER.strip() + "\n"):
        text = text.split("\n", 1)[1]
    entries = []
    for paragraph in re.split(r"\n\s*\n|\n(?=- )", text.strip()):
        if not paragraph.strip() or paragraph.strip() == _HEADER.strip():
            continue
        content = re.sub(r"^\s*[-*]\s+", "", paragraph).strip()
        category = "project_context"
        match = re.match(r"^\[(偏好|项目|经验|任务)\]\s*", content)
        if match:
            category = _CATEGORIES[match[1]]
            content = content[match.end():]
        content = re.sub(r"\n {2}", "\n", content).strip()
        if content == "[已清除]":
            continue
        if not content or len(content) > 4000:
            raise GovernanceError("每条记忆须为 1–4000 字符，请用空行分隔")
        if re.search(r"密码|api.?key|secret|sk-[A-Za-z0-9]{12}|银行卡|信用卡", content, re.I):
            raise GovernanceError("请勿在记忆文本保存敏感凭据")
        if (category, content) not in entries:
            entries.append((category, content))
    return entries


def _reconcile(service, text, scope):
    from .governance import _content_id
    from .schema import MemoryType
    entries = _parse(text)
    previous = _records(service, scope)
    # Validate all blocked IDs before writing any records.
    for category, content in entries:
        kind = {"preference": MemoryType.PROCEDURAL, "project_context": MemoryType.SEMANTIC}.get(category, MemoryType.EPISODIC)
        mid = _content_id(content, kind, scope)
        old = service.store.get(mid)
        if service.store.is_tombstoned(mid) or old and old.state == MemoryState.REJECTED:
            raise GovernanceError("文本含已删除或拒绝的记忆，请修改后再保存")
    kept = set()
    for category, content in entries:
        same = next((r for r in previous if service.content_for(r.memory_id) == content and r.category == category), None)
        if same:
            kept.add(same.memory_id)
            continue
        kind = {"preference": MemoryType.PROCEDURAL, "project_context": MemoryType.SEMANTIC}.get(category, MemoryType.EPISODIC)
        old = service.store.get(_content_id(content, kind, scope))
        if old and old.state == MemoryState.DISABLED and not old.superseded_by:
            record = service.enable(old.memory_id)
        else:
            record = service.save_explicit(content, category=category, scope=scope, source_thread="memory_document")
        kept.add(record.memory_id)
    for record in previous:
        current = service.store.get(record.memory_id)
        if record.memory_id not in kept and current.state == MemoryState.ACTIVE:
            service.disable(record.memory_id, actor="memory_document")


def _publish(service, scope):
    path, state_path = _paths(scope)
    records = _records(service, scope)
    content = _text(service, records)
    digest = _digest(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    for target, body in ((path, content), (state_path, json.dumps({"hash": digest, "records": _signature(records)}))):
        if target.exists() and target.read_text(encoding="utf-8") == body:
            continue
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(target)
    return {"content": content, "revision": digest, "path": str(path), "scope": scope}


def read_document(service, scope="global"):
    with service.store._lock:
        path, state_path = _paths(scope)
        if path.is_file():
            with path.open(encoding="utf-8") as source:
                external = source.read(32001)
            if len(external) > 32000:
                raise GovernanceError("记忆文本最多 32000 字符，请分到不同项目范围")
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if _digest(external) != state.get("hash"):
                signature = _signature(_records(service, scope))
                if state and state.get("records") != signature:
                    return {"content": external, "revision": _digest(external + signature),
                            "scope": scope, "path": str(path), "conflict": True,
                            "server_content": _text(service, _records(service, scope))}
                _reconcile(service, external, scope)
        return _publish(service, scope)


def write_document(service, content, revision, scope="global"):
    with service.store._lock:
        current = read_document(service, scope)
        if current["revision"] != revision:
            raise GovernanceConflictError("记忆已更新，请重新加载后保存")
        _reconcile(service, content, scope)
        return _publish(service, scope)


def context_records(service, scope="global"):
    """Refresh direct file edits, but never fail a chat on a document conflict."""
    from .types import RetrievalResult
    from ..personal import memory_allowed
    with service.store._lock:
        for name in dict.fromkeys(("global", scope)) if memory_allowed(write=True) else ():
            try:
                read_document(service, name)
            except (GovernanceError, OSError, ValueError):
                # Preserve the user's file and fall back to the governed records.
                pass
        records = _records(service, scope)
        if scope != "global":
            records += _records(service, "global")
        return [RetrievalResult(trace=trace, similarity=1, strength=trace.strength, score=1)
                for record in records if (trace := service.trace_store.get(record.memory_id))]
