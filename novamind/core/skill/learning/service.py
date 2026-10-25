"""One workspace learning service shared by desktop, Web and CLI."""
from __future__ import annotations

import time

from .jobs import LearningJobs
from .observer import LearningObserver
from .policy import LearningError, redact_source
from .publisher import Publisher
from .store import LearningStore


class LearningService:
    def __init__(self, skills, history_reader, root, factory=None, busy=None):
        self.store = LearningStore(skills, root)
        self.store.source_validator = self.valid_source
        self.store.purge_revoked_files()
        self.history_reader = history_reader
        self.observer = LearningObserver(self.store)
        self.publisher = Publisher(self.store)
        self.jobs = LearningJobs(self.store, self.sources, factory, busy)

    @staticmethod
    def valid_source(observation):
        ids = observation.get("evidence", {}).get("memory_ids", [])
        if not ids:
            return True
        try:
            from ...memory.governance_runtime import get_governance_service
            governance = get_governance_service()
            for memory_id in ids:
                record = governance.store.get(memory_id)
                if not record or record.state.value != "active" or record.superseded_by or not record.user_confirmed:
                    return False
            return True
        except Exception:
            raise LearningError("暂时无法核对来源记忆，已停止使用相关方法", 503) from None

    def refresh_sources(self):
        with self.store.tx() as conn:
            observations = [self.store.get(conn, "observations", row["id"]) for row in
                            conn.execute("SELECT id FROM learning_observations WHERE state='active' LIMIT 5000").fetchall()]
        invalid = set()
        for observation in observations:
            try:
                if not self.valid_source(observation):
                    invalid.add(observation["id"])
            except LearningError:
                continue  # Temporary storage failure hides use, without permanently revoking facts.
        if invalid:
            self.store.revoke(lambda observation: observation["id"] in invalid)

    def sources(self, candidate):
        with self.store.tx() as conn:
            self.store.check_candidate(conn, candidate)
            observations = [self.store.get(conn, "observations", sid) for sid in candidate["sources"]]
        result, remaining = [], 8000
        for observation in observations:
            messages = self.history_reader().load_learning_text(observation["thread_id"], observation["message_ids"])
            if not messages:
                raise LearningError("来源会话已删除或无法读取", 409)
            for text in messages:
                if remaining <= 0:
                    break
                result.append(redact_source(text[:remaining]))
                remaining -= len(result[-1])
        return result

    def source_preview(self, pattern_id):
        sources = self.store.observations(pattern_id)
        result = []
        for source in sources[:10]:
            result.append({**source, "preview": self.sources({"sources": [source["id"]], "scope": source["scope"]})})
        return result

    def feedback(self, run_id, skill_id, value, revision=None):
        if value not in {"helpful", "problem", "unknown"}:
            raise LearningError("反馈无效")
        with self.store.tx() as conn:
            rows = conn.execute("SELECT id FROM learning_observations WHERE state='active'").fetchall()
            match = next((item for row in rows if (item := self.store.get(conn, "observations", row["id"]))["run_id"] == run_id
                          and skill_id in item.get("evidence", {}).get("skills", [])), None)
            if not match:
                raise LearningError("未找到该轮真实技能使用记录", 404)
            if revision is not None and match["revision"] != revision:
                raise LearningError("反馈版本已变化", 409)
            match.setdefault("feedback", {})[skill_id] = {"value": value, "source": "user", "time": time.time()}
            return self.store.put(conn, "observations", match, expected=match["revision"])

    def usage(self, thread_id):
        with self.store.tx() as conn:
            rows = conn.execute("SELECT id FROM learning_observations WHERE state='active' ORDER BY created DESC LIMIT 1000").fetchall()
            for row in rows:
                item = self.store.get(conn, "observations", row["id"])
                if item["thread_id"] == thread_id and item.get("evidence", {}).get("skills"):
                    return {"run_id": item["run_id"], "revision": item["revision"], "created": item["created"], "skills": [
                        {"id": sid, "title": record["description"], "feedback": item.get("feedback", {}).get(sid, {})}
                        for sid in item["evidence"]["skills"] if (record := conn.execute(
                            "SELECT description FROM skill_records WHERE skill_id=?", (sid,)).fetchone())]}
        return {"skills": []}

    def acknowledge_risk(self, job_id):
        job = self.jobs.read(job_id)
        if job["state"] in {"queued", "running", "cancel_requested"} or not job.get("reserved"):
            raise LearningError("该调用仍在进行或没有未知费用，不能确认清零", 409)
        return self.jobs.update(job_id, risk_acknowledged=True)

    def clear_observations(self):
        self.observer.generation += 1
        with self.store.tx() as conn:
            self.store.put(conn, "policy_events", {"id": "observations_cleared", "state": "cleared", "scope": "global"})
        self.store.revoke(lambda item: True)
        with self.store.tx() as conn:
            conn.execute("DELETE FROM learning_observations WHERE id NOT IN (SELECT observation_id FROM learning_source_links)")
        return {"status": "ok"}

    async def shutdown(self):
        jobs_stopped = await self.jobs.shutdown()
        import asyncio
        observer_stopped = await asyncio.to_thread(self.observer.close)
        return jobs_stopped and observer_stopped
