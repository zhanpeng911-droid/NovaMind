"""Learning data shares the registry transaction; drafts never register skills."""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .policy import LearningError, digest, skill_name, static_check, template

KINDS = ("observations", "patterns", "candidates", "jobs", "evaluations", "policy_events", "skills")


def identifier(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


class LearningStore:
    def __init__(self, skills, root: Path):
        self.skills = skills
        self.root = Path(root)
        self.source_validator: Callable[[dict], bool] | None = None
        with self.tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS learning_schema (version INTEGER PRIMARY KEY)")
            for kind in KINDS:
                conn.execute(f"CREATE TABLE IF NOT EXISTS learning_{kind} "
                             "(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, "
                             "scope TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_learning_{kind}_scope ON learning_{kind}(scope,updated)")
            conn.execute("CREATE TABLE IF NOT EXISTS learning_source_links "
                         "(candidate_id TEXT NOT NULL, observation_id TEXT NOT NULL, PRIMARY KEY(candidate_id,observation_id))")
            conn.execute("INSERT OR IGNORE INTO learning_schema VALUES (1)")

    def tx(self):
        return self.skills.learning_transaction()

    @staticmethod
    def _table(kind):
        if kind not in KINDS:
            raise ValueError("unknown learning record")
        return "learning_" + kind

    def get(self, conn, kind, key):
        row = conn.execute(f"SELECT * FROM {self._table(kind)} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise LearningError("记录不存在", 404)
        return {**json.loads(row["payload"]), "id": row["id"], "revision": row["revision"],
                "state": row["state"], "scope": row["scope"], "created": row["created"], "updated": row["updated"]}

    def put(self, conn, kind, item, *, expected=None):
        key = item["id"]
        old = conn.execute(f"SELECT revision,created FROM {self._table(kind)} WHERE id=?", (key,)).fetchone()
        if expected is not None and (old is None or old["revision"] != expected):
            raise LearningError("版本已更新，请重新读取后再操作", 409)
        revision = old["revision"] + 1 if old else 1
        now = time.time()
        value = {**item, "revision": revision, "updated": now, "created": old["created"] if old else now}
        conn.execute(f"INSERT INTO {self._table(kind)} VALUES (?,?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,state=excluded.state,"
                     "scope=excluded.scope,payload=excluded.payload,updated=excluded.updated",
                     (key, revision, value.get("state", "active"), value.get("scope", "global"),
                      json.dumps(value, ensure_ascii=False), value["created"], now))
        return value

    def items(self, kind, *, limit=50, offset=0):
        with self.tx() as conn:
            keys = conn.execute(f"SELECT id FROM {self._table(kind)} ORDER BY updated DESC,id LIMIT ? OFFSET ?",
                                (min(100, max(1, limit)), max(0, offset))).fetchall()
            return [self.get(conn, kind, row["id"]) for row in keys]

    def observe(self, run_id, thread_id, message_ids, scope, hints, evidence):
        if not hints:
            return None
        key = "obs_" + digest(thread_id + "\0" + run_id)[:32]
        pattern = "pattern_" + digest(scope + "\0" + hints["family"])[:24]
        with self.tx() as conn:
            cleared = conn.execute("SELECT updated FROM learning_policy_events WHERE id='observations_cleared'").fetchone()
            if cleared and evidence.get("submitted_at", time.time()) <= cleared["updated"]:
                return None
            revoked = ["thread:" + thread_id] + ["memory:" + str(mid) for mid in evidence.get("memory_ids", [])]
            if any(conn.execute("SELECT 1 FROM learning_policy_events WHERE id=?", ("revoked_" + digest(key),)).fetchone()
                   for key in revoked):
                return None
            if conn.execute("SELECT 1 FROM learning_observations WHERE id=?", (key,)).fetchone():
                return self.get(conn, "observations", key)
            existing = conn.execute("SELECT payload FROM learning_observations WHERE scope=? AND state='active'",
                                    (scope,)).fetchall()
            duplicate = any((previous := json.loads(row["payload"])).get("fingerprint") == hints["fingerprint"]
                            and previous.get("thread_id") == thread_id and previous.get("created", 0) > time.time() - 300
                            for row in existing)
            conn.execute("INSERT OR IGNORE INTO learning_patterns VALUES (?,?,?,?,?,?,?)",
                         (pattern, 1, "active", scope, json.dumps({"label": hints["label"]}, ensure_ascii=False), time.time(), time.time()))
            return self.put(conn, "observations", {"id": key, "state": "active", "scope": scope,
                "run_id": run_id, "thread_id": thread_id, "message_ids": message_ids[:12],
                "pattern_id": pattern, "family": hints["family"], "fingerprint": hints["fingerprint"],
                "evidence": evidence, "outcome": "unknown", "duplicate_hint": duplicate})

    def patterns(self, minimum=3):
        result = []
        for pattern in self.items("patterns", limit=100):
            observations = [o for o in self.observations(pattern["id"]) if not o.get("duplicate_hint") and o["family"] != "personal"]
            if (pattern["state"] == "active" and len(observations) >= minimum
                    and len({o["fingerprint"] for o in observations}) >= 2 and pattern.get("cooldown_until", 0) <= time.time()):
                result.append({**pattern, "count": len(observations), "outcome": "unknown"})
        return result

    def observations(self, pattern_id):
        # Kept bounded by retention; avoid collecting any conversation body here.
        with self.tx() as conn:
            rows = conn.execute("SELECT id FROM learning_observations WHERE state='active' ORDER BY created DESC LIMIT 1000").fetchall()
            return [item for row in rows if (item := self.get(conn, "observations", row["id"]))["pattern_id"] == pattern_id]

    def source_check(self, conn, source_ids, scope):
        for source_id in source_ids:
            source = self.get(conn, "observations", source_id)
            if source["state"] != "active" or source["scope"] != scope:
                raise LearningError("来源已失效或不属于当前范围", 409)
            if self.source_validator is not None and not self.source_validator(source):
                raise LearningError("来源依赖的记忆已变化，请重新审阅", 409)

    def check_candidate(self, conn, candidate):
        self.source_check(conn, candidate["sources"], candidate.get("source_scope", candidate["scope"]))

    def create_candidate(self, title, scope, source_ids, *, baseline_id=None, global_confirmed=False):
        if len(source_ids) > 10 or len(title) > 80 or not title.strip() or not scope or len(scope) > 120:
            raise LearningError("名称、范围或示例数量无效")
        source_ids = list(dict.fromkeys(source_ids))
        with self.tx() as conn:
            source_scope = self.get(conn, "observations", source_ids[0])["scope"] if source_ids else scope
            self.source_check(conn, source_ids, source_scope)
            if scope != source_scope and not (scope == "global" and global_confirmed):
                raise LearningError("跨范围方法需要明确选择并确认全局适用", 409)
            body = template(title)
            result = self.put(conn, "candidates", {"id": identifier("candidate"), "state": "draft", "scope": scope,
                "title": title, "name": skill_name(title, scope), "body": body, "hash": digest(body),
                "baseline_id": baseline_id, "sources": source_ids, "source_scope": source_scope,
                "evaluation": "not_run", "checks": []})
            for sid in source_ids:
                conn.execute("INSERT INTO learning_source_links VALUES (?,?)", (result["id"], sid))
            return result

    def candidate(self, key):
        with self.tx() as conn:
            return self.get(conn, "candidates", key)

    def edit_candidate(self, key, revision, body):
        if len(body) > 16000:
            raise LearningError("技能正文过长")
        with self.tx() as conn:
            item = self.get(conn, "candidates", key)
            if item["state"] not in {"draft", "review_ready"}:
                raise LearningError("该候选不可编辑，请新建版本", 409)
            self.check_candidate(conn, item)
            errors = static_check(body)
            item.pop("behavior", None)
            item.update(body=body, hash=digest(body), evaluation="static_failed" if errors else "static_pass",
                        checks=errors, state="draft" if errors else "review_ready")
            return self.put(conn, "candidates", item, expected=revision)

    def dismiss(self, pattern_id, revision):
        with self.tx() as conn:
            item = self.get(conn, "patterns", pattern_id)
            item["state"] = "dismissed"
            return self.put(conn, "patterns", item, expected=revision)

    def revoke_thread(self, thread_id):
        self.suppress_source("thread:" + thread_id)
        self.revoke(lambda item: item.get("thread_id") == thread_id)

    def revoke_memory(self, memory_id):
        self.suppress_source("memory:" + memory_id)
        self.revoke(lambda item: memory_id in item.get("evidence", {}).get("memory_ids", []))

    def suppress_source(self, source):
        with self.tx() as conn:
            self.put(conn, "policy_events", {"id": "revoked_" + digest(source), "state": "revoked", "scope": "global"})

    def reject(self, key, revision):
        with self.tx() as conn:
            item = self.get(conn, "candidates", key)
            if item["state"] not in {"draft", "review_ready"}:
                raise LearningError("该候选不可拒绝", 409)
            item.update(state="rejected", body="")
            result = self.put(conn, "candidates", item, expected=revision)
            for source in item["sources"]:
                observation = self.get(conn, "observations", source)
                pattern = self.get(conn, "patterns", observation["pattern_id"])
                pattern["state"] = "dismissed"
                self.put(conn, "patterns", pattern)
            return result

    def revoke(self, predicate):
        with self.tx() as conn:
            rows = conn.execute("SELECT id FROM learning_observations WHERE state='active'").fetchall()
            for row in rows:
                item = self.get(conn, "observations", row["id"])
                if not predicate(item):
                    continue
                item["state"] = "revoked"
                self.put(conn, "observations", item)
                links = conn.execute("SELECT candidate_id FROM learning_source_links WHERE observation_id=?", (item["id"],)).fetchall()
                for link in links:
                    candidate = self.get(conn, "candidates", link["candidate_id"])
                    candidate["state"] = "revoked"
                    candidate["body"] = ""
                    self.put(conn, "candidates", candidate)
                    skill_id = candidate.get("published_skill_id")
                    if skill_id:
                        conn.execute("UPDATE skill_records SET enabled=0 WHERE skill_id=?", (skill_id,))
                        skill = self.get(conn, "skills", skill_id)
                        skill["state"] = "revoked"
                        self.put(conn, "skills", skill)
        self.purge_revoked_files()

    def purge_revoked_files(self):
        """Unlink only the application's own version files; retain revocation metadata."""
        with self.tx() as conn:
            rows = conn.execute("SELECT sr.skill_id,sr.path FROM skill_records sr JOIN learning_skills ls "
                                "ON sr.skill_id=ls.id WHERE ls.state='revoked'").fetchall()
        failures = 0
        root = self.root.resolve()
        for row in rows:
            path = Path(row["path"])
            try:
                if path.name != "SKILL.md" or not path.resolve().is_relative_to(root / "learned_skills"):
                    raise OSError("invalid managed path")
                path.unlink(missing_ok=True)
            except OSError:
                failures += 1
            with self.tx() as conn:
                self.put(conn, "policy_events", {"id": "purge_" + digest(row["skill_id"]), "state": "pending" if path.exists() else "cleared",
                                                "scope": "global", "skill_id": row["skill_id"]})
        return failures

    def prune(self, days=30, maximum=1000):
        with self.tx() as conn:
            rows = conn.execute("SELECT id,created FROM learning_observations WHERE id NOT IN "
                                "(SELECT observation_id FROM learning_source_links) ORDER BY created DESC").fetchall()
            for index, row in enumerate(rows):
                if index >= maximum or row["created"] < time.time() - days * 86400:
                    conn.execute("DELETE FROM learning_observations WHERE id=?", (row["id"],))
