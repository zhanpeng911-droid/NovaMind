"""SQLiteSkillStore — SQLite + WAL + version DAG + 4 计数器打点。

吸收 Poirot `skill/store.py`：
- 内容/索引分离：只存 path + content_hash，SKILL.md 全文留文件
- WAL 模式 + busy_timeout=30000 + threading.Lock 保护写
- is_active 单指针：每 name 仅 1 active，回滚切指针不删除
- 4 计数器 programmatic 打点（零 LLM）
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import SkillHealth, SkillLineage, SkillMetrics, SkillRecord

_SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_records (
    skill_id            TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    path                TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    generation          INTEGER NOT NULL DEFAULT 0,
    origin              TEXT NOT NULL DEFAULT 'IMPORTED',
    created_by          TEXT,
    description         TEXT NOT NULL DEFAULT '',
    allowed_tools       TEXT NOT NULL DEFAULT '[]',
    enabled             INTEGER NOT NULL DEFAULT 1,
    total_selections    INTEGER NOT NULL DEFAULT 0,
    total_applied       INTEGER NOT NULL DEFAULT 0,
    total_completions   INTEGER NOT NULL DEFAULT 0,
    total_fallbacks     INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    last_updated        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sr_name   ON skill_records(name);
CREATE INDEX IF NOT EXISTS idx_sr_active ON skill_records(is_active);

CREATE TABLE IF NOT EXISTS skill_lineage_parents (
    skill_id        TEXT NOT NULL,
    parent_skill_id TEXT NOT NULL,
    PRIMARY KEY (skill_id, parent_skill_id)
);

CREATE TABLE IF NOT EXISTS skill_judgments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    skill_id        TEXT NOT NULL,
    applied         INTEGER,
    task_completed  INTEGER NOT NULL DEFAULT 0,
    ts              TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sj_skill ON skill_judgments(skill_id);

CREATE TABLE IF NOT EXISTS skill_evolutions (
    evolution_id        TEXT PRIMARY KEY,
    skill_name          TEXT NOT NULL,
    evolution_type      TEXT NOT NULL,
    trigger             TEXT NOT NULL,
    baseline_id         TEXT,
    candidate_id        TEXT NOT NULL,
    failure_focus       TEXT NOT NULL DEFAULT '',
    mutation_diff       TEXT NOT NULL DEFAULT '',
    eval_score          REAL NOT NULL DEFAULT 0.0,
    gate_decision       TEXT NOT NULL,
    created_version_id  TEXT,
    timestamp           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_se_skill ON skill_evolutions(skill_name, timestamp);

CREATE TABLE IF NOT EXISTS skill_eval_judgments (
    judgment_id    TEXT PRIMARY KEY,
    skill_id       TEXT NOT NULL,
    skill_name     TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    skill_applied  INTEGER NOT NULL,
    deviation_note TEXT NOT NULL DEFAULT '',
    timestamp      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sej_skill ON skill_eval_judgments(skill_id, timestamp);

CREATE TABLE IF NOT EXISTS task_quality_scores (
    score_id         TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    task_completion  REAL NOT NULL,
    response_quality REAL NOT NULL,
    efficiency       REAL NOT NULL,
    tool_usage       REAL NOT NULL,
    overall_score    REAL NOT NULL,
    rationale        TEXT NOT NULL DEFAULT '',
    timestamp        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tqs_task ON task_quality_scores(task_id);

CREATE TABLE IF NOT EXISTS skill_eval_runs (
    eval_run_id   TEXT PRIMARY KEY,
    eval_layer    TEXT NOT NULL,
    skill_ids     TEXT NOT NULL,
    candidate_id  TEXT,
    baseline_id   TEXT,
    result_json   TEXT NOT NULL DEFAULT '',
    timestamp     TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteSkillStore:
    """skill 基础层存储。SQLite + WAL + version DAG + 4 计数器。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._mu:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    # ── 注册 / 发现 ──────────────────────────────────────────

    def register(self, record: SkillRecord) -> str:
        with self._mu:
            self._conn.execute(
                """INSERT OR IGNORE INTO skill_records
                     (skill_id, name, path, content_hash, is_active, generation, origin,
                      created_by, description, allowed_tools, enabled, created_at, last_updated)
                   VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?)""",
                (record.skill_id, record.name, record.path, record.content_hash,
                 record.lineage.generation, record.lineage.origin, record.lineage.created_by,
                 record.description, json.dumps(list(record.allowed_tools)),
                 1 if record.enabled else 0, _now_iso(), _now_iso()),
            )
            if record.lineage.parent_skill_ids:
                for pid in record.lineage.parent_skill_ids:
                    self._conn.execute("INSERT OR IGNORE INTO skill_lineage_parents VALUES (?,?)", (record.skill_id, pid))
            self._conn.commit()
            return record.skill_id

    def get(self, skill_id: str) -> SkillRecord | None:
        with self._mu:
            row = self._conn.execute("SELECT * FROM skill_records WHERE skill_id=?", (skill_id,)).fetchone()
            return self._row_to_record(row) if row else None

    def get_active(self, name: str) -> SkillRecord | None:
        with self._mu:
            row = self._conn.execute("SELECT * FROM skill_records WHERE name=? AND is_active=1", (name,)).fetchone()
            return self._row_to_record(row) if row else None

    def list_active(self) -> list[SkillRecord]:
        with self._mu:
            rows = self._conn.execute("SELECT * FROM skill_records WHERE is_active=1").fetchall()
            return [self._row_to_record(r) for r in rows]

    def set_enabled(self, skill_id: str, enabled: bool) -> bool:
        with self._mu:
            cur = self._conn.execute("UPDATE skill_records SET enabled=? WHERE skill_id=?", (1 if enabled else 0, skill_id))
            self._conn.commit()
            return cur.rowcount > 0

    def discover(self, dirs: list[Path], origin: str = "IMPORTED") -> list[SkillRecord]:
        from .parser import parse_skill_file

        results: list[SkillRecord] = []
        for d in dirs:
            for skill_md in Path(d).rglob("SKILL.md"):
                record = parse_skill_file(skill_md, origin=origin)
                self._upsert_record(record)
                results.append(record)
        return results

    def _upsert_record(self, record: SkillRecord) -> None:
        with self._mu:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO skill_records
                     (skill_id, name, path, content_hash, is_active, generation, origin,
                      created_by, description, allowed_tools, enabled, created_at, last_updated)
                   VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?)""",
                (record.skill_id, record.name, record.path, record.content_hash,
                 record.lineage.generation, record.lineage.origin, record.lineage.created_by,
                 record.description, json.dumps(list(record.allowed_tools)),
                 1 if record.enabled else 0, _now_iso(), _now_iso()),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    """UPDATE skill_records SET path=?, content_hash=?, description=?,
                         allowed_tools=?, enabled=?, last_updated=? WHERE skill_id=?""",
                    (record.path, record.content_hash, record.description,
                     json.dumps(list(record.allowed_tools)), 1 if record.enabled else 0,
                     _now_iso(), record.skill_id),
                )
            if record.lineage.parent_skill_ids:
                for pid in record.lineage.parent_skill_ids:
                    self._conn.execute("INSERT OR IGNORE INTO skill_lineage_parents VALUES (?,?)", (record.skill_id, pid))
            self._conn.commit()

    # ── version DAG ──────────────────────────────────────────

    def create_version(self, parent_id: str, record: SkillRecord, origin: str) -> str:
        with self._mu:
            ts = _now_iso()
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO skill_records
                     (skill_id, name, path, content_hash, is_active, generation, origin,
                      created_by, description, allowed_tools, enabled, created_at, last_updated)
                   VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?)""",
                (record.skill_id, record.name, record.path, record.content_hash,
                 record.lineage.generation, origin, record.lineage.created_by,
                 record.description, json.dumps(list(record.allowed_tools)),
                 1 if record.enabled else 0, ts, ts),
            )
            if cur.rowcount == 0:
                raise ValueError(f"skill_id already exists: {record.skill_id}")
            self._conn.execute("UPDATE skill_records SET is_active=0 WHERE name=? AND skill_id<>?", (record.name, record.skill_id))
            if parent_id:
                self._conn.execute("INSERT OR IGNORE INTO skill_lineage_parents VALUES (?,?)", (record.skill_id, parent_id))
            self._conn.commit()
            return record.skill_id

    def get_versions(self, name: str) -> list[SkillRecord]:
        with self._mu:
            rows = self._conn.execute("SELECT * FROM skill_records WHERE name=? ORDER BY generation ASC", (name,)).fetchall()
            return [self._row_to_record(r) for r in rows]

    def rollback(self, skill_id: str) -> None:
        with self._mu:
            row = self._conn.execute("SELECT name FROM skill_records WHERE skill_id=?", (skill_id,)).fetchone()
            if row is None:
                return
            name = row["name"]
            self._conn.execute("UPDATE skill_records SET is_active=1 WHERE skill_id=?", (skill_id,))
            self._conn.execute("UPDATE skill_records SET is_active=0 WHERE name=? AND skill_id<>?", (name, skill_id))
            self._conn.commit()

    # ── quality metrics 打点 ─────────────────────────────────

    def record_selection(self, skill_id: str) -> None:
        with self._mu:
            self._conn.execute(
                "UPDATE skill_records SET total_selections = total_selections + 1, last_updated = ? WHERE skill_id = ?",
                (_now_iso(), skill_id),
            )
            self._conn.commit()

    def record_outcome(self, skill_id: str, run_id: str, applied: bool | None, task_completed: bool, note: str = "") -> None:
        with self._mu:
            exists = self._conn.execute("SELECT 1 FROM skill_records WHERE skill_id=?", (skill_id,)).fetchone()
            if exists is None:
                return
            inc_applied = 1 if applied is True else 0
            inc_completion = 1 if (applied is True and task_completed) else 0
            inc_fallback = 1 if (applied is False and not task_completed) else 0
            self._conn.execute(
                "UPDATE skill_records SET total_applied = total_applied + ?, "
                "total_completions = total_completions + ?, total_fallbacks = total_fallbacks + ?, "
                "last_updated = ? WHERE skill_id = ?",
                (inc_applied, inc_completion, inc_fallback, _now_iso(), skill_id),
            )
            self._conn.execute(
                "INSERT INTO skill_judgments (run_id, skill_id, applied, task_completed, ts, note) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, skill_id, None if applied is None else (1 if applied else 0),
                 1 if task_completed else 0, _now_iso(), note),
            )
            self._conn.commit()

    def get_metrics(self, skill_id: str) -> SkillMetrics | None:
        with self._mu:
            row = self._conn.execute(
                "SELECT total_selections, total_applied, total_completions, total_fallbacks FROM skill_records WHERE skill_id=?",
                (skill_id,),
            ).fetchone()
            if row is None:
                return None
            sel, app, comp, fb = row["total_selections"], row["total_applied"], row["total_completions"], row["total_fallbacks"]
            return SkillMetrics(
                skill_id=skill_id, selections=sel, applied=app, completions=comp, fallbacks=fb,
                applied_rate=app / sel if sel else 0.0,
                completion_rate=comp / app if app else 0.0,
                effective_rate=comp / sel if sel else 0.0,
                fallback_rate=fb / sel if sel else 0.0,
            )

    def get_top_skills(self, n: int, metric: str = "effective_rate", min_selections: int = 5) -> list[SkillRecord]:
        with self._mu:
            rows = self._conn.execute(
                "SELECT * FROM skill_records WHERE is_active=1 AND total_selections >= ?", (min_selections,)
            ).fetchall()
            records = [self._row_to_record(r) for r in rows]
            records.sort(key=lambda r: getattr(r, metric), reverse=True)
            return records[:n]

    def health_check(self, threshold: float = 0.4, min_selections: int = 5) -> list[SkillHealth]:
        results: list[SkillHealth] = []
        for rec in self.list_active():
            degraded = rec.total_selections >= min_selections and rec.effective_rate < threshold
            results.append(SkillHealth(
                skill_id=rec.skill_id, name=rec.name,
                effective_rate=rec.effective_rate, fallback_rate=rec.fallback_rate,
                total_selections=rec.total_selections, degraded=degraded,
            ))
        return results

    # ── evolution 记录 ───────────────────────────────────────

    def record_evolution(self, record: Any) -> str:
        with self._mu:
            self._conn.execute(
                """INSERT OR REPLACE INTO skill_evolutions
                   (evolution_id, skill_name, evolution_type, trigger, baseline_id, candidate_id,
                    failure_focus, mutation_diff, eval_score, gate_decision, created_version_id, timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.evolution_id, record.skill_name, record.evolution_type, record.trigger,
                 record.baseline_id, record.candidate_id, record.failure_focus, record.mutation_diff,
                 record.eval_score, record.gate_decision, record.created_version_id, record.timestamp or _now_iso()),
            )
            self._conn.commit()
            return record.evolution_id

    def get_evolution_history(self, skill_name: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._mu:
            rows = self._conn.execute(
                "SELECT * FROM skill_evolutions WHERE skill_name=? ORDER BY timestamp DESC LIMIT ?", (skill_name, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── eval 持久化 ──────────────────────────────────────────

    def save_judgment(self, judgment: Any) -> str:
        with self._mu:
            self._conn.execute(
                """INSERT OR REPLACE INTO skill_eval_judgments
                   (judgment_id, skill_id, skill_name, task_id, skill_applied, deviation_note, timestamp)
                   VALUES (?,?,?,?,?,?,?)""",
                (judgment.judgment_id, judgment.skill_id, judgment.skill_name, judgment.task_id,
                 1 if judgment.skill_applied else 0, judgment.deviation_note, judgment.timestamp or _now_iso()),
            )
            self._conn.commit()
            return judgment.judgment_id

    def get_judgments(self, skill_id: str, limit: int = 20) -> list[Any]:
        from .eval.types import SkillJudgment

        with self._mu:
            rows = self._conn.execute(
                "SELECT * FROM skill_eval_judgments WHERE skill_id=? ORDER BY timestamp DESC LIMIT ?", (skill_id, limit)
            ).fetchall()
        return [
            SkillJudgment(
                judgment_id=r["judgment_id"], skill_id=r["skill_id"], skill_name=r["skill_name"],
                task_id=r["task_id"], skill_applied=bool(r["skill_applied"]),
                deviation_note=r["deviation_note"], timestamp=r["timestamp"],
            )
            for r in rows
        ]

    def save_task_score(self, score: Any) -> str:
        with self._mu:
            self._conn.execute(
                """INSERT OR REPLACE INTO task_quality_scores
                   (score_id, task_id, task_completion, response_quality, efficiency, tool_usage, overall_score, rationale, timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (score.score_id, score.task_id, score.task_completion, score.response_quality,
                 score.efficiency, score.tool_usage, score.overall_score, score.rationale, score.timestamp or _now_iso()),
            )
            self._conn.commit()
            return score.score_id

    def get_task_scores(self, task_id: str) -> Any | None:
        from .eval.types import TaskQualityScore

        with self._mu:
            row = self._conn.execute("SELECT * FROM task_quality_scores WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskQualityScore(
            score_id=row["score_id"], task_id=row["task_id"],
            task_completion=row["task_completion"], response_quality=row["response_quality"],
            efficiency=row["efficiency"], tool_usage=row["tool_usage"],
            overall_score=row["overall_score"], rationale=row["rationale"], timestamp=row["timestamp"],
        )

    def save_eval_run(self, run: Any) -> str:
        with self._mu:
            self._conn.execute(
                """INSERT OR REPLACE INTO skill_eval_runs
                   (eval_run_id, eval_layer, skill_ids, candidate_id, baseline_id, result_json, timestamp)
                   VALUES (?,?,?,?,?,?,?)""",
                (run.eval_run_id, run.eval_layer, json.dumps(list(run.skill_ids)),
                 run.candidate_id, run.baseline_id, run.result_json, run.timestamp or _now_iso()),
            )
            self._conn.commit()
            return run.eval_run_id

    # ── helpers ──────────────────────────────────────────────

    def _row_to_record(self, row: sqlite3.Row) -> SkillRecord:
        parent_rows = self._conn.execute(
            "SELECT parent_skill_id FROM skill_lineage_parents WHERE skill_id=?", (row["skill_id"],)
        ).fetchall()
        parents = tuple(r["parent_skill_id"] for r in parent_rows)
        lineage = SkillLineage(
            parent_skill_ids=parents, generation=row["generation"],
            origin=row["origin"], version_hash=row["content_hash"], created_by=row["created_by"],
        )
        return SkillRecord(
            skill_id=row["skill_id"], name=row["name"], path=row["path"], content_hash=row["content_hash"],
            is_active=bool(row["is_active"]), lineage=lineage, description=row["description"],
            allowed_tools=tuple(json.loads(row["allowed_tools"])), enabled=bool(row["enabled"]),
            total_selections=row["total_selections"], total_applied=row["total_applied"],
            total_completions=row["total_completions"], total_fallbacks=row["total_fallbacks"],
            created_at=row["created_at"], last_updated=row["last_updated"],
        )

    def close(self) -> None:
        with self._mu:
            self._conn.close()
            self._conn = None
