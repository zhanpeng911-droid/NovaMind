"""Reviewed publication is a transaction; immutable files precede DB pointers."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import yaml

from .policy import LearningError, digest, static_check
from .store import identifier


@contextmanager
def unpublished_file(path, root):
    try:
        yield
    except BaseException:
        try:
            if path.resolve().is_relative_to(root):
                path.unlink(missing_ok=True)
        except OSError:
            import logging
            logging.getLogger(__name__).warning("unreferenced personal skill file needs cleanup")
        raise


class Publisher:
    def __init__(self, store):
        self.store = store

    def publish(self, key, revision, body_hash, expected_base, mode, enable=False, cooldown_days=7):
        candidate = self.store.candidate(key)
        if candidate["state"] == "published" and candidate["hash"] == body_hash and candidate.get("approved_revision") == revision:
            return candidate["publication"]
        self._validate(candidate, revision, body_hash, mode)
        skill_id = identifier(candidate["name"])
        directory = self.store.root / "learned_skills" / candidate["name"] / skill_id
        root = (self.store.root / "learned_skills").resolve()
        if not root.is_relative_to(self.store.root.resolve()) or not directory.resolve().is_relative_to(root):
            raise LearningError("技能路径无效")
        directory.mkdir(parents=True, exist_ok=False)
        content = "---\n" + yaml.safe_dump({"name": candidate["name"], "description": candidate["title"],
                                            "allowed-tools": []}, allow_unicode=True) + "---\n\n" + candidate["body"]
        temporary, path = directory / "SKILL.tmp", directory / "SKILL.md"
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
        file_hash = digest(content)[:16]
        with unpublished_file(path, root), self.store.tx() as conn:
            current = self.store.get(conn, "candidates", key)
            self._validate(current, revision, body_hash, mode)
            self.store.check_candidate(conn, current)
            old = conn.execute("SELECT skill_id,generation FROM skill_records WHERE name=? AND is_active=1", (current["name"],)).fetchone()
            active_id = old["skill_id"] if old else None
            if active_id != expected_base or current.get("baseline_id") != expected_base:
                raise LearningError("基线版本已变化，请重新整理并审阅", 409)
            stamp = datetime.now(timezone.utc).isoformat()
            conn.execute("INSERT INTO skill_records (skill_id,name,path,content_hash,is_active,generation,origin,created_by,"
                         "description,allowed_tools,enabled,created_at,last_updated) VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?)",
                         (skill_id, current["name"], str(path), file_hash, old["generation"] + 1 if old else 0,
                          "PERSONAL_LEARNED", "user_review", current["title"], "[]", int(enable), stamp, stamp))
            conn.execute("UPDATE skill_records SET is_active=0 WHERE name=? AND skill_id<>?", (current["name"], skill_id))
            if active_id:
                conn.execute("INSERT INTO skill_lineage_parents VALUES (?,?)", (skill_id, active_id))
            publication = {"skill_id": skill_id, "mode": mode, "enabled": enable}
            current.update(state="published", approved_revision=revision, published_skill_id=skill_id, publication=publication)
            self.store.put(conn, "candidates", current, expected=revision)
            self.store.put(conn, "skills", {"id": skill_id, "scope": current["scope"], "state": "active",
                "candidate_id": key, "body_hash": body_hash, "file_hash": file_hash, "mode": mode})
            self.store.put(conn, "policy_events", {"id": identifier("event"), "state": "published", "scope": current["scope"],
                "candidate_id": key, "revision_approved": revision, "hash": body_hash, "skill_id": skill_id})
            for sid in current["sources"]:
                source = self.store.get(conn, "observations", sid)
                pattern = self.store.get(conn, "patterns", source["pattern_id"])
                pattern["cooldown_until"] = time.time() + cooldown_days * 86400
                self.store.put(conn, "patterns", pattern)
        return publication

    @staticmethod
    def _validate(candidate, revision, body_hash, mode):
        if candidate["revision"] != revision or candidate["hash"] != body_hash:
            raise LearningError("版本或内容已变化，请重新审阅", 409)
        if candidate["state"] != "review_ready" or static_check(candidate["body"]):
            raise LearningError("候选尚未通过静态检查", 409)
        if mode not in {"trial", "verified"}:
            raise LearningError("请选择试用或已验证模式")
        if candidate["evaluation"] in {"static_failed", "behavior_fail"}:
            raise LearningError("验证失败的候选不能启用", 409)
        if mode == "verified" and candidate["evaluation"] != "behavior_pass":
            raise LearningError("尚无行为验证，不能标记为已验证", 409)

    def rollback(self, skill_id, expected_active):
        with self.store.tx() as conn:
            target = self.store.get(conn, "skills", skill_id)
            candidate = self.store.get(conn, "candidates", target["candidate_id"])
            self.store.check_candidate(conn, candidate)
            row = conn.execute("SELECT * FROM skill_records WHERE skill_id=?", (skill_id,)).fetchone()
            if target["state"] != "active" or not row:
                raise LearningError("该版本已被撤销", 409)
            from pathlib import Path
            path = Path(row["path"])
            if not path.resolve().is_relative_to(self.store.root.resolve() / "learned_skills"):
                raise LearningError("版本文件路径无效", 409)
            try:
                with path.open(encoding="utf-8") as source:
                    content = source.read(20001)
            except OSError:
                raise LearningError("版本文件已失效", 409) from None
            if len(content) > 20000 or digest(content)[:16] != row["content_hash"]:
                raise LearningError("版本文件已变化，无法回退", 409)
            active = conn.execute("SELECT skill_id,enabled FROM skill_records WHERE name=? AND is_active=1", (row["name"],)).fetchone()
            if not active or active["skill_id"] != expected_active:
                raise LearningError("当前版本已变化", 409)
            conn.execute("UPDATE skill_records SET is_active=0 WHERE name=?", (row["name"],))
            conn.execute("UPDATE skill_records SET is_active=1,enabled=? WHERE skill_id=?", (active["enabled"], skill_id))
            self.store.put(conn, "policy_events", {"id": identifier("event"), "scope": target["scope"], "state": "rollback",
                                                  "skill_id": skill_id, "previous_id": expected_active})
        return {"skill_id": skill_id}

    def revoke(self, skill_id):
        with self.store.tx() as conn:
            skill = self.store.get(conn, "skills", skill_id)
            skill["state"] = "revoked"
            self.store.put(conn, "skills", skill)
            conn.execute("UPDATE skill_records SET enabled=0 WHERE skill_id=?", (skill_id,))
            candidate = self.store.get(conn, "candidates", skill["candidate_id"])
            candidate.update(state="revoked", body="")
            self.store.put(conn, "candidates", candidate)
        return {"status": "ok", "pending_file_cleanup": self.store.purge_revoked_files()}
