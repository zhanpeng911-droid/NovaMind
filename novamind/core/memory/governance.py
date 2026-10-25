"""记忆治理层（B0）— 生命周期状态、版本屏障与墓碑。

职责边界：
- 正文事实源仍是 MarkdownFileStore（traces.md），本模块不复制正文；
- 本模块只持久化「生命周期/治理元数据」（独立 SQLite 索引）；
- 缺少治理记录的正文不召回；历史记忆只在受控迁移中登记一次；
- 删除写 tombstone（永久屏障），后台 worker 不得复活；
- 候选（Proposed）在用户确认前不进入召回。

INVARIANT:
- 召回只允许 ACTIVE、未被 tombstone、未被 supersede 的记录；
- tombstone 后 worker 的 propose/encode 被拒绝；
- 所有状态变更追加 revision 审计；
- 治理库按操作短连接（Windows 文件锁安全）。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .schema import MemoryTrace, MemoryType

logger = logging.getLogger(__name__)


class MemoryState(str, Enum):
    """记忆生命周期状态。"""

    PROPOSED = "proposed"
    ACTIVE = "active"
    REJECTED = "rejected"
    DISABLED = "disabled"
    DELETED = "deleted"


TRUST_USER = "user"
TRUST_CONVERSATION = "conversation"
TRUST_UNTRUSTED = "untrusted"

_CATEGORY_BY_TYPE = {
    MemoryType.PROCEDURAL: "preference",
    MemoryType.SEMANTIC: "project_context",
    MemoryType.EPISODIC: "experience",
}


class GovernanceError(ValueError):
    """治理层基础异常。"""


class GovernanceConflictError(GovernanceError):
    """revision 前置检查失败（并发覆盖）。"""


class GovernancePermissionError(GovernanceError):
    """不可信来源不得被直接确认为长期记忆。"""


def infer_category(trace: MemoryTrace, override: str | None = None) -> str:
    """逻辑分类：metadata 显式声明优先，否则按现有 MemoryType 映射。"""
    if override:
        return override
    meta_category = trace.metadata.get("category") if trace.metadata else None
    if isinstance(meta_category, str) and meta_category:
        return meta_category
    return _CATEGORY_BY_TYPE.get(trace.type, "general")


@dataclass(frozen=True)
class GovernanceRecord:
    """一条记忆的治理元数据（不含正文）。"""

    memory_id: str
    category: str
    state: MemoryState
    revision: int
    source_thread_id: str | None = None
    source_turn_id: str | None = None
    model_proposed: bool = False
    user_confirmed: bool = False
    trust_level: str = TRUST_USER
    scope: str = "global"
    created_at: float = 0.0
    updated_at: float = 0.0
    confirmed_at: float | None = None
    superseded_by: str | None = None


class MemoryGovernanceStore:
    """治理元数据 SQLite 存储（每操作短连接 + 进程内锁）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 连接与建表 ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_governance (
                        memory_id TEXT PRIMARY KEY,
                        category TEXT NOT NULL,
                        state TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        source_thread_id TEXT,
                        source_turn_id TEXT,
                        model_proposed INTEGER NOT NULL DEFAULT 0,
                        user_confirmed INTEGER NOT NULL DEFAULT 0,
                        trust_level TEXT NOT NULL DEFAULT 'user',
                        scope TEXT NOT NULL DEFAULT 'global',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        confirmed_at REAL,
                        superseded_by TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_revision (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        from_state TEXT,
                        to_state TEXT NOT NULL,
                        reason TEXT,
                        actor TEXT,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_tombstone (
                        memory_id TEXT PRIMARY KEY,
                        reason TEXT,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # ── 读 ───────────────────────────────────────────────────────────

    def epoch(self, bump: bool = False) -> int:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS memory_control (key TEXT PRIMARY KEY, value INTEGER)")
                conn.execute("INSERT OR IGNORE INTO memory_control VALUES ('epoch', 0)")
                if bump:
                    conn.execute("UPDATE memory_control SET value=value+1 WHERE key='epoch'")
                value = conn.execute("SELECT value FROM memory_control WHERE key='epoch'").fetchone()[0]
                conn.commit()
                return int(value)
            finally:
                conn.close()

    def get(self, memory_id: str) -> GovernanceRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT memory_id, category, state, revision, source_thread_id,
                              source_turn_id, model_proposed, user_confirmed, trust_level,
                              scope, created_at, updated_at, confirmed_at, superseded_by
                       FROM memory_governance WHERE memory_id = ?""",
                    (memory_id,),
                ).fetchone()
            finally:
                conn.close()
        return _row_to_record(row) if row else None

    def is_tombstoned(self, memory_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM memory_tombstone WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
            finally:
                conn.close()
        return row is not None

    def list_records(self, states: Iterable[MemoryState] | None = None) -> list[GovernanceRecord]:
        state_values = [s.value if isinstance(s, MemoryState) else str(s) for s in states] if states else []
        sql = "SELECT * FROM memory_governance"
        params: tuple[Any, ...] = ()
        if state_values:
            placeholders = ",".join("?" for _ in state_values)
            sql += f" WHERE state IN ({placeholders})"
            params = tuple(state_values)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [_row_to_record(_reorder_row(r)) for r in rows]

    def count_by_state(self) -> dict[str, int]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT state, COUNT(*) FROM memory_governance GROUP BY state"
                ).fetchall()
            finally:
                conn.close()
        return {str(state): int(count) for state, count in rows}

    # ── 写 ───────────────────────────────────────────────────────────

    def upsert(self, record: GovernanceRecord, *, reason: str = "", actor: str = "system") -> GovernanceRecord:
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT revision, state FROM memory_governance WHERE memory_id = ?",
                    (record.memory_id,),
                ).fetchone()
                if existing is not None:
                    from_state = str(existing[1])
                    if record.revision <= int(existing[0]):
                        raise GovernanceConflictError(
                            f"stale revision for {record.memory_id}: "
                            f"{record.revision} <= {existing[0]}"
                        )
                else:
                    from_state = None
                conn.execute(
                    """INSERT INTO memory_governance (
                           memory_id, category, state, revision, source_thread_id,
                           source_turn_id, model_proposed, user_confirmed, trust_level,
                           scope, created_at, updated_at, confirmed_at, superseded_by
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(memory_id) DO UPDATE SET
                           category=excluded.category,
                           state=excluded.state,
                           revision=excluded.revision,
                           source_thread_id=excluded.source_thread_id,
                           source_turn_id=excluded.source_turn_id,
                           model_proposed=excluded.model_proposed,
                           user_confirmed=excluded.user_confirmed,
                           trust_level=excluded.trust_level,
                           scope=excluded.scope,
                           updated_at=excluded.updated_at,
                           confirmed_at=excluded.confirmed_at,
                           superseded_by=excluded.superseded_by""",
                    (
                        record.memory_id, record.category, record.state.value,
                        record.revision, record.source_thread_id, record.source_turn_id,
                        int(record.model_proposed), int(record.user_confirmed),
                        record.trust_level, record.scope, record.created_at,
                        record.updated_at, record.confirmed_at, record.superseded_by,
                    ),
                )
                self._append_revision(conn, record.memory_id, record.revision,
                                      from_state, record.state.value, reason, actor)
                conn.commit()
            finally:
                conn.close()
        return record

    def tombstone(self, memory_id: str, *, reason: str = "user_deleted", actor: str = "user") -> None:
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_tombstone (memory_id, reason, created_at) VALUES (?,?,?)",
                    (memory_id, reason, now),
                )
                conn.commit()
            finally:
                conn.close()

    def _append_revision(self, conn: sqlite3.Connection, memory_id: str, revision: int,
                         from_state: str | None, to_state: str, reason: str, actor: str) -> None:
        conn.execute(
            """INSERT INTO memory_revision
               (memory_id, revision, from_state, to_state, reason, actor, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (memory_id, revision, from_state, to_state, reason, actor, time.time()),
        )

    def revisions(self, memory_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """SELECT revision, from_state, to_state, reason, actor, created_at
                       FROM memory_revision WHERE memory_id = ? ORDER BY id ASC""",
                    (memory_id,),
                ).fetchall()
            finally:
                conn.close()
        return [
            {"revision": r[0], "from_state": r[1], "to_state": r[2],
             "reason": r[3], "actor": r[4], "created_at": r[5]}
            for r in rows
        ]

    def close(self) -> None:
        """无持久连接，保留接口。"""
        return None


def _row_to_record(row: tuple) -> GovernanceRecord:
    return GovernanceRecord(
        memory_id=row[0], category=row[1], state=MemoryState(row[2]),
        revision=int(row[3]), source_thread_id=row[4], source_turn_id=row[5],
        model_proposed=bool(row[6]), user_confirmed=bool(row[7]),
        trust_level=row[8], scope=row[9], created_at=float(row[10]),
        updated_at=float(row[11]), confirmed_at=row[12], superseded_by=row[13],
    )


def _reorder_row(row: tuple) -> tuple:
    """list_records 用 SELECT *，列顺序与 _row_to_record 一致（建表顺序）。"""
    return row


def _content_id(content: str, mem_type: MemoryType, scope: str = "global") -> str:
    """Keep legacy global IDs; namespace project records to prevent scope collisions."""
    raw = f"{content}\x00{mem_type.value}"
    if scope != "global":
        raw += "\x00scope:" + scope
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class MemoryGovernanceService:
    """生命周期服务：候选/确认/拒绝/禁用/删除/冲突/迁移/召回过滤。"""

    def __init__(self, store: MemoryGovernanceStore, trace_store: Any | None = None,
                 retriever: Any | None = None) -> None:
        self._store = store
        self._trace_store = trace_store
        self._retriever = retriever

    @property
    def store(self) -> MemoryGovernanceStore:
        return self._store

    # ── 状态查询 ─────────────────────────────────────────────────────

    def effective_state(self, memory_id: str) -> MemoryState:
        """未登记正文默认关闭；tombstone 优先。"""
        if self._store.is_tombstoned(memory_id):
            return MemoryState.DELETED
        record = self._store.get(memory_id)
        return record.state if record else MemoryState.PROPOSED

    def is_recallable(self, memory_id: str) -> bool:
        if self._store.is_tombstoned(memory_id):
            return False
        record = self._store.get(memory_id)
        if record is None:
            return False
        return record.state == MemoryState.ACTIVE and not record.superseded_by

    def filter_recallable(
        self,
        results: list[Any],
        *,
        scope: str | None = None,
        min_score: float = 0.0,
        allowed_categories: set[str] | None = None,
    ) -> list[Any]:
        """过滤检索结果（RetrievalResult 需有 .trace.id / .score）。

        - 只保留 ACTIVE、非 tombstone、非 superseded；
        - scope：记录 scope 为 global 或等于当前 scope 才注入；
        - 信任：untrusted 且未经用户确认的永不注入；
        - 相关性门槛：score < min_score 丢弃（B2）。
        """
        filtered: list[Any] = []
        for result in results:
            if getattr(result, "score", 1.0) < min_score:
                continue
            memory_id = result.trace.id
            if self._store.is_tombstoned(memory_id):
                continue
            record = self._store.get(memory_id)
            if record is None:
                continue
            if record.state != MemoryState.ACTIVE or record.superseded_by:
                continue
            if record.scope not in ("global", scope or "global"):
                continue
            if record.trust_level == TRUST_UNTRUSTED and not record.user_confirmed:
                continue
            if allowed_categories and record.category not in allowed_categories:
                continue
            filtered.append(result)
        return filtered

    def recall_preferences(self, text: str, *, top_k: int = 5,
                           min_score: float = 0.0) -> list[str]:
        """只取必要偏好（Quick Action 用），不加载项目上下文/任务经验。"""
        from ..personal import memory_allowed, request_context
        if self._retriever is None or not memory_allowed():
            return []
        from .types import MemoryQuery

        results = self._retriever.retrieve(
            MemoryQuery(text=text, top_k=top_k, type_filter=MemoryType.PROCEDURAL)
        )
        scope = request_context.get().get("scope", "global")
        results = self.standing_preferences(text, scope) + results
        filtered = self.filter_recallable(
            results, scope=scope, min_score=min_score, allowed_categories={"preference"},
        )
        return list(dict.fromkeys(r.trace.content for r in filtered))[:top_k]

    def standing_preferences(self, text: str, scope: str = "global") -> list[Any]:
        """Explicit expression preferences apply across turns without a lexical hit."""
        import re
        from .types import RetrievalResult
        results = []
        code_task = bool(re.search(r"代码|编程|函数|报错|python|javascript|code|bug|实现", text, re.I))
        with self._store._lock:
            for record in self.list_governed(states=[MemoryState.ACTIVE], category="preference"):
                if not record.user_confirmed or record.scope not in ("global", scope):
                    continue
                trace = self._trace_store.get(record.memory_id)
                if trace is None:
                    continue
                general = re.search(r"(回答|回复|沟通).*(中文|英文|简洁|简短|详细)|用(中文|英文).*(回答|回复)", trace.content)
                coding = code_task and re.search(r"思路|示例|代码|编程", trace.content)
                if general or coding:
                    results.append(RetrievalResult(trace=trace, similarity=1, strength=trace.strength, score=1))
                if len(results) >= 3:
                    break
            return self.filter_recallable(results, scope=scope)

    def blocked_for_worker(self, memory_id: str) -> bool:
        """worker 写入前的硬屏障：墓碑、已删除、已拒绝、用户禁用都不得复活。"""
        if self._store.is_tombstoned(memory_id):
            return True
        record = self._store.get(memory_id)
        return record is not None and record.state in (
            MemoryState.DELETED, MemoryState.REJECTED, MemoryState.DISABLED,
        )

    # ── 生命周期操作 ─────────────────────────────────────────────────

    def propose(
        self,
        trace: MemoryTrace,
        *,
        source_thread_id: str | None = None,
        source_turn_id: str | None = None,
        category: str | None = None,
        scope: str = "global",
        model_proposed: bool = True,
        trust_level: str = TRUST_CONVERSATION,
        reason: str = "model_proposed",
        actor: str = "worker",
    ) -> GovernanceRecord:
        """写入候选。墓碑/已删除/已拒绝 → 拒绝复活；已 ACTIVE 不降级。"""
        now = time.time()
        if self._store.is_tombstoned(trace.id):
            raise GovernanceError(f"memory is tombstoned: {trace.id}")
        existing = self._store.get(trace.id)
        if existing is not None:
            if existing.state in (MemoryState.ACTIVE, MemoryState.DELETED):
                return existing
            if existing.state in (MemoryState.REJECTED, MemoryState.DISABLED) or existing.superseded_by:
                raise GovernanceError(f"memory is blocked: {trace.id}")
            revision = existing.revision + 1
            created_at = existing.created_at
            user_confirmed = existing.user_confirmed
            confirmed_at = existing.confirmed_at
        else:
            revision = 1
            created_at = now
            user_confirmed = False
            confirmed_at = None
        record = GovernanceRecord(
            memory_id=trace.id,
            category=infer_category(trace, category),
            state=MemoryState.PROPOSED,
            revision=revision,
            source_thread_id=source_thread_id,
            source_turn_id=source_turn_id,
            model_proposed=model_proposed,
            user_confirmed=user_confirmed,
            trust_level=trust_level,
            scope=scope,
            created_at=created_at,
            updated_at=now,
            confirmed_at=confirmed_at,
            superseded_by=None,
        )
        return self._store.upsert(record, reason=reason, actor=actor)

    def confirm(self, memory_id: str, *, revision: int | None = None,
                force: bool = False, actor: str = "user") -> GovernanceRecord:
        """确认候选为 ACTIVE。不可信来源必须 force=True（显式用户操作）。"""
        record = self._store.get(memory_id)
        if record is None:
            raise GovernanceError(f"no governance record for {memory_id}")
        if self._store.is_tombstoned(memory_id) or record.state in (
            MemoryState.DELETED, MemoryState.REJECTED,
        ):
            raise GovernanceError(f"memory is not confirmable: {memory_id}")
        if record.superseded_by:
            raise GovernanceError("旧版本已被替代，请使用当前有效记忆")
        if record.trust_level == TRUST_UNTRUSTED and not force:
            raise GovernancePermissionError(
                f"untrusted source cannot be confirmed without explicit force: {memory_id}"
            )
        if revision is not None and record.revision != revision:
            raise GovernanceConflictError(
                f"revision mismatch: expected {revision}, got {record.revision}"
            )
        now = time.time()
        updated = GovernanceRecord(
            **{**record.__dict__,
               "state": MemoryState.ACTIVE,
               "revision": record.revision + 1,
               "user_confirmed": True,
               "confirmed_at": now,
               "updated_at": now,
               "superseded_by": None}
        )
        return self._store.upsert(updated, reason="user_confirmed", actor=actor)

    def reject(self, memory_id: str, *, actor: str = "user") -> GovernanceRecord:
        record = self._require(memory_id)
        now = time.time()
        updated = GovernanceRecord(**{**record.__dict__, "state": MemoryState.REJECTED,
                                      "revision": record.revision + 1,
                                      "updated_at": now, "superseded_by": None})
        return self._store.upsert(updated, reason="user_rejected", actor=actor)

    def disable(self, memory_id: str, *, actor: str = "user") -> GovernanceRecord:
        record = self._require(memory_id)
        if self._store.is_tombstoned(memory_id) or record.state in (
            MemoryState.DELETED, MemoryState.REJECTED,
        ):
            raise GovernanceError(f"memory cannot be disabled: {memory_id}")
        now = time.time()
        updated = GovernanceRecord(**{**record.__dict__, "state": MemoryState.DISABLED,
                                      "revision": record.revision + 1,
                                      "updated_at": now})
        return self._store.upsert(updated, reason="user_disabled", actor=actor)

    def enable(self, memory_id: str, *, actor: str = "user") -> GovernanceRecord:
        record = self._require(memory_id)
        if self._store.is_tombstoned(memory_id):
            raise GovernanceError(f"memory is deleted: {memory_id}")
        if record.state != MemoryState.DISABLED:
            raise GovernanceError(f"memory is not disabled: {memory_id}")
        if record.superseded_by:
            raise GovernanceError("旧版本已被替代，请编辑当前有效版本")
        now = time.time()
        updated = GovernanceRecord(**{**record.__dict__, "state": (MemoryState.ACTIVE if record.user_confirmed else MemoryState.PROPOSED),
                                      "revision": record.revision + 1,
                                      "updated_at": now})
        return self._store.upsert(updated, reason="user_enabled", actor=actor)

    def delete(self, memory_id: str, *, reason: str = "user_deleted",
               actor: str = "user") -> None:
        """删除：tombstone 永久屏障 + 内容标记 forgotten（检索层双保险）。"""
        record = self._store.get(memory_id)
        self._store.epoch(bump=True)
        now = time.time()
        self._store.tombstone(memory_id, reason=reason, actor=actor)
        if record is not None:
            updated = GovernanceRecord(**{**record.__dict__, "state": MemoryState.DELETED,
                                          "revision": record.revision + 1,
                                          "updated_at": now, "superseded_by": None})
            self._store.upsert(updated, reason=reason, actor=actor)
        else:
            # 无记录的历史记忆：建 DELETED 记录，保持状态可见
            self._store.upsert(GovernanceRecord(
                memory_id=memory_id, category="general", state=MemoryState.DELETED,
                revision=1, created_at=now, updated_at=now, user_confirmed=True,
                trust_level=TRUST_USER,
            ), reason=reason, actor=actor)
        self._mark_trace_forgotten(memory_id)

    def supersede(self, old_id: str, new_id: str, *, actor: str = "user") -> GovernanceRecord:
        """旧值作废、新值生效：旧记录 DISABLED + superseded_by，避免双注入。"""
        old = self._require(old_id)
        new = self._require(new_id)
        now = time.time()
        disabled = GovernanceRecord(**{**old.__dict__, "state": MemoryState.DISABLED,
                                       "revision": old.revision + 1,
                                       "updated_at": now, "superseded_by": new_id})
        self._store.upsert(disabled, reason=f"superseded_by:{new_id}", actor=actor)
        active = GovernanceRecord(**{**new.__dict__, "state": MemoryState.ACTIVE,
                                     "revision": new.revision + 1,
                                     "user_confirmed": True, "confirmed_at": now,
                                     "updated_at": now, "superseded_by": None})
        return self._store.upsert(active, reason=f"supersedes:{old_id}", actor=actor)

    def note_consolidated(self, old_ids: list[str], new_trace: MemoryTrace,
                          *, actor: str = "worker") -> GovernanceRecord:
        """合并产物仅作为候选，不自动替换仍有效的源记忆。"""
        existing = self._store.get(new_trace.id)
        if existing is not None and existing.state in (MemoryState.ACTIVE, MemoryState.DELETED):
            return existing
        return self.propose(
            new_trace, category=infer_category(new_trace),
            model_proposed=True, trust_level=TRUST_CONVERSATION,
            reason="consolidated", actor=actor,
        )

    # ── 迁移 ─────────────────────────────────────────────────────────

    def migrate_legacy(self, traces: Iterable[MemoryTrace]) -> int:
        """为无治理记录的历史记忆补 ACTIVE（forgotten 补 DISABLED），幂等。"""
        now = time.time()
        migrated = 0
        for trace in traces:
            if self._store.get(trace.id) is not None:
                continue
            state = MemoryState.DISABLED if trace.metadata.get("forgotten") else MemoryState.ACTIVE
            record = GovernanceRecord(
                memory_id=trace.id,
                category=infer_category(trace),
                state=state,
                revision=1,
                source_thread_id=trace.source,
                source_turn_id=None,
                model_proposed=False,
                user_confirmed=True,
                trust_level="legacy",
                scope="global",
                created_at=trace.created_at or now,
                updated_at=now,
                confirmed_at=trace.created_at or now,
                superseded_by=None,
            )
            self._store.upsert(record, reason="legacy_migration", actor="system")
            migrated += 1
        return migrated

    # ── Memory Center 查询/编辑（B1） ───────────────────────────────

    @property
    def trace_store(self) -> Any:
        return self._trace_store

    def list_governed(
        self,
        *,
        states: Iterable[MemoryState] | None = None,
        category: str | None = None,
        scope: str | None = None,
    ) -> list[GovernanceRecord]:
        records = self._store.list_records(states)
        if category:
            records = [r for r in records if r.category == category]
        if scope:
            records = [r for r in records if r.scope == scope]
        return sorted(records, key=lambda r: (r.updated_at, r.memory_id), reverse=True)

    def content_for(self, memory_id: str) -> str:
        if self._store.is_tombstoned(memory_id):
            return ""
        if self._trace_store is None:
            return ""
        trace = self._trace_store.get(memory_id)
        return trace.content if trace is not None else ""

    def save_explicit(self, content: str, *, category: str = "preference", scope: str = "global",
                      confirmed: bool = True, source_thread: str | None = None,
                      source_turn: str | None = None, importance: float = 0.5) -> GovernanceRecord:
        """Register a closed candidate BEFORE publishing its body, then optionally confirm."""
        import re
        from ..personal import request_context, memory_allowed
        if not memory_allowed(write=True):
            raise GovernanceError("本次对话不记录个人记忆")
        content = content.strip()
        if not content or len(content) > 4000:
            raise GovernanceError("记忆正文须为 1–4000 字符")
        if re.search(r"密码|api.?key|secret|sk-[A-Za-z0-9]{12}|银行卡|信用卡", content, re.I):
            raise GovernanceError("未保存可能包含敏感凭据的内容")
        kind = {"preference": MemoryType.PROCEDURAL, "project_context": MemoryType.SEMANTIC}.get(category, MemoryType.EPISODIC)
        import math
        from .config import get_memory_config
        if not math.isfinite(importance) or not 0 <= importance <= 1:
            raise GovernanceError("记忆重要度须为 0–1")
        decay = get_memory_config().decay[kind.value]
        now = time.time()
        ctx = request_context.get()
        trace = MemoryTrace(id=_content_id(content, kind, scope), content=content, type=kind,
                            strength=decay["base_strength"], base_strength=decay["base_strength"],
                            decay_rate=decay["decay_rate"], importance=importance,
                            created_at=now, last_accessed=now,
                            source=source_thread or ctx.get("thread_id", "user"),
                            metadata={"category": category, "governed": True})
        with self._store._lock:
            if confirmed and category == "preference":
                # Resolve common explicit corrections by their stable meaning,
                # not their body hash. Other topics use the correction tool.
                def topic(value):
                    for key, pattern in (("code_explanation_order", r"(思路|示例).*(先|再)|先.*(思路|示例)"),
                                         ("answer_language", r"(回答|回复|沟通).*(中文|英文)|用(中文|英文).*(回答|回复)"),
                                         ("answer_length", r"(回答|回复).*(简短|简洁|详细)|以后.*(简短|简洁|详细)")):
                        if re.search(pattern, value):
                            return key
                    return None
                key = topic(content)
                if key:
                    for previous in self.list_governed(states=[MemoryState.ACTIVE], category="preference", scope=scope):
                        old_content = self.content_for(previous.memory_id)
                        if previous.memory_id != trace.id and topic(old_content) == key:
                            return self.update_content(previous.memory_id, content,
                                                       expected_revision=previous.revision)
            self.propose(trace, category=category, scope=scope, model_proposed=not confirmed,
                         trust_level=TRUST_USER if confirmed else TRUST_CONVERSATION,
                         source_thread_id=source_thread or ctx.get("thread_id"),
                         source_turn_id=source_turn or ctx.get("turn_id"))
            if self._trace_store.get(trace.id) is None:
                self._trace_store.add(trace)
            if confirmed:
                return self.confirm(trace.id)
            return self._store.get(trace.id)

    def revisions_for(self, memory_id: str) -> list[dict[str, Any]]:
        return self._store.revisions(memory_id)

    def update_content(
        self,
        memory_id: str,
        new_content: str,
        *,
        expected_revision: int,
        category: str | None = None,
        scope: str | None = None,
        actor: str = "user",
    ) -> GovernanceRecord:
        """编辑正文：内容寻址 id 会变化 → 新 trace 生效、旧 trace forgotten/禁用。

        保持 revision 前置检查；旧版本仍可在 operation_log/traces.md 回溯。
        """
        record = self._require(memory_id)
        if self._store.is_tombstoned(memory_id):
            raise GovernanceError(f"memory is deleted: {memory_id}")
        if record.revision != expected_revision:
            raise GovernanceConflictError(
                f"revision mismatch: expected {expected_revision}, got {record.revision}"
            )
        self._store.epoch(bump=True)
        if self._trace_store is None:
            raise GovernanceError("trace store unavailable")
        trace = self._trace_store.get(memory_id)
        if trace is None:
            raise GovernanceError(f"trace not found: {memory_id}")

        new_content = (new_content or "").strip()
        import re
        if not new_content or len(new_content) > 4000:
            raise GovernanceError("记忆正文须为 1–4000 字符")
        if re.search(r"密码|api.?key|secret|sk-[A-Za-z0-9]{12}|银行卡|信用卡", new_content, re.I):
            raise GovernanceError("未保存可能包含敏感凭据的内容")

        now = time.time()
        new_id = _content_id(new_content, trace.type, scope or record.scope)
        if new_id == memory_id:
            updated = GovernanceRecord(
                **{**record.__dict__,
                   "category": category or record.category,
                   "scope": scope or record.scope,
                   "revision": record.revision + 1,
                   "updated_at": now}
            )
            return self._store.upsert(updated, reason="user_edited", actor=actor)

        if self._store.is_tombstoned(new_id):
            raise GovernanceError(f"edited content is deleted: {new_id}")
        existing = self._store.get(new_id)
        if existing is not None and existing.state != MemoryState.DELETED:
            raise GovernanceConflictError(f"edited content already exists: {new_id}")

        from dataclasses import replace as _replace

        new_trace = _replace(
            trace,
            id=new_id,
            content=new_content,
            created_at=now,
            last_accessed=now,
            metadata={
                k: v for k, v in (trace.metadata or {}).items()
                if k not in ("forgotten", "governance_state", "superseded_by")
            },
        )
        self.propose(new_trace, category=category or record.category, scope=scope or record.scope,
                     model_proposed=False, trust_level=TRUST_USER)
        self._trace_store.add(new_trace)
        self._trace_store.update(_replace(
            trace,
            metadata={**(trace.metadata or {}), "forgotten": True,
                      "superseded_by": new_id},
        ))

        disabled = _replace(
            record, state=MemoryState.DISABLED,
            revision=record.revision + 1, updated_at=now, superseded_by=new_id,
        )
        self._store.upsert(disabled, reason=f"superseded_by:{new_id}", actor=actor)

        new_record = GovernanceRecord(
            memory_id=new_id,
            category=category or record.category,
            state=MemoryState.ACTIVE,
            revision=self._store.get(new_id).revision + 1,
            source_thread_id=record.source_thread_id,
            source_turn_id=record.source_turn_id,
            model_proposed=False,
            user_confirmed=True,
            trust_level=TRUST_USER,
            scope=scope or record.scope,
            created_at=now,
            updated_at=now,
            confirmed_at=now,
            superseded_by=None,
        )
        return self._store.upsert(new_record, reason="user_edited", actor=actor)

    def update_metadata(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        category: str | None = None,
        scope: str | None = None,
        actor: str = "user",
    ) -> GovernanceRecord:
        record = self._require(memory_id)
        if record.revision != expected_revision:
            raise GovernanceConflictError(
                f"revision mismatch: expected {expected_revision}, got {record.revision}"
            )
        now = time.time()
        updated = GovernanceRecord(
            **{**record.__dict__,
               "category": category or record.category,
               "scope": scope or record.scope,
               "revision": record.revision + 1,
               "updated_at": now}
        )
        return self._store.upsert(updated, reason="user_edited_metadata", actor=actor)

    # ── 内部 ─────────────────────────────────────────────────────────

    def _require(self, memory_id: str) -> GovernanceRecord:
        record = self._store.get(memory_id)
        if record is None:
            raise GovernanceError(f"no governance record for {memory_id}")
        return record

    def _mark_trace_forgotten(self, memory_id: str) -> None:
        if self._trace_store is None:
            return
        trace = self._trace_store.get(memory_id)
        if trace is None:
            return
        from dataclasses import replace

        updated = replace(trace, metadata={**(trace.metadata or {}), "forgotten": True,
                                           "governance_state": MemoryState.DELETED.value})
        self._trace_store.update(updated)


# A single service lock covers file and index transitions, including readers.
# Unregistered bodies stay closed even when a process stops between writes.
def _serialize_governance(method, invalidate=False):
    from functools import wraps

    @wraps(method)
    def run(self, *args, **kwargs):
        with self._store._lock:
            if invalidate:
                self._store.epoch(bump=True)
            return method(self, *args, **kwargs)
    return run


for _name in ("save_explicit", "propose", "confirm", "reject", "disable", "enable", "delete",
              "supersede", "update_content", "update_metadata", "filter_recallable"):
    setattr(MemoryGovernanceService, _name, _serialize_governance(
        getattr(MemoryGovernanceService, _name),
        invalidate=_name in {"reject", "disable", "delete", "supersede", "update_content", "update_metadata"},
    ))
