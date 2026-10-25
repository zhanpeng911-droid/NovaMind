"""No network: scripted models prove accounting, cancellation and stale results."""
import asyncio
from types import SimpleNamespace

import pytest

from novamind.core.skill import SQLiteSkillStore
from novamind.core.skill.learning.jobs import LearningJobs
from novamind.core.skill.learning.store import LearningStore

BUDGET = {"budget_yuan": 1., "input_price": 1., "output_price": 1., "provider": "other", "model": "offline"}
BODY = "\n".join("## " + title + "\n根据用户提供的材料执行并核对，不扩大权限。" for title in
                 ["适用任务", "不适用条件", "输入参数", "执行步骤", "输出格式", "完成检查", "常见失败与处理"])


@pytest.fixture
def store(tmp_path):
    skills = SQLiteSkillStore(tmp_path / "skills.sqlite3")
    yield LearningStore(skills, tmp_path)
    skills.close()


class Model:
    def __init__(self, usage=True):
        self.calls = 0
        self.usage = usage

    async def ainvoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=BODY, usage_metadata={"input_tokens": 20, "output_tokens": 30} if self.usage else {})


@pytest.mark.asyncio
async def test_generate_settles_real_usage_without_publishing(store):
    model = Model()
    jobs = LearningJobs(store, lambda candidate: ["selected data"], lambda cfg: model)
    candidate = store.create_candidate("论文方法", "global", [])
    job = jobs.submit(candidate["id"], 1, "generate", "same", BUDGET)
    assert jobs.submit(candidate["id"], 1, "generate", "same", BUDGET)["id"] == job["id"]
    await jobs.tasks[job["id"]]
    final = jobs.read(job["id"])
    assert final["state"] == "completed"
    assert final["spent"] == pytest.approx(.00005)
    assert final["reserved"] == 0
    assert model.calls == 1
    assert store.candidate(candidate["id"])["evaluation"] == "static_pass"
    assert store.skills.list_active() == []
    await jobs.shutdown()


@pytest.mark.asyncio
async def test_missing_usage_stops_admission_and_survives_reopening(store):
    model = Model(usage=False)
    jobs = LearningJobs(store, lambda c: [], lambda cfg: model)
    candidate = store.create_candidate("方法", "global", [])
    job = jobs.submit(candidate["id"], 1, "generate", "unknown", BUDGET)
    await jobs.tasks[job["id"]]
    assert jobs.read(job["id"])["reserved"] > 0
    assert jobs.read(job["id"])["state"] == "failed"
    reopened = LearningJobs(store, lambda c: [], lambda cfg: model)
    with pytest.raises(ValueError, match="未知"):
        reopened.submit(candidate["id"], 1, "generate", "another", BUDGET)
    assert model.calls == 1
    await jobs.shutdown()
    await reopened.shutdown()


@pytest.mark.asyncio
async def test_insufficient_budget_and_cancelled_queue_make_no_calls(store):
    model = Model()
    jobs = LearningJobs(store, lambda c: [], lambda cfg: model)
    candidate = store.create_candidate("方法", "global", [])
    job = jobs.submit(candidate["id"], 1, "generate", "tiny", {**BUDGET, "budget_yuan": 1e-12})
    await jobs.tasks[job["id"]]
    assert model.calls == 0
    queued = jobs.submit(candidate["id"], 1, "generate", "cancel", BUDGET)
    task = jobs.tasks[queued["id"]]
    jobs.cancel(queued["id"])
    await asyncio.gather(task, return_exceptions=True)
    assert model.calls == 0
    assert jobs.read(queued["id"])["state"] == "cancelled"
    await jobs.shutdown()


@pytest.mark.asyncio
async def test_edited_candidate_rejects_late_model_result(store):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def ainvoke(self, messages):
            started.set()
            await release.wait()
            return await super().ainvoke(messages)

    jobs = LearningJobs(store, lambda c: [], lambda cfg: Slow())
    candidate = store.create_candidate("方法", "global", [])
    job = jobs.submit(candidate["id"], 1, "generate", "late", BUDGET)
    task = jobs.tasks[job["id"]]
    await started.wait()
    edited = store.edit_candidate(candidate["id"], 1, BODY + "\n用户的新修改。")
    release.set()
    await task
    assert jobs.read(job["id"])["state"] == "failed"
    assert store.candidate(candidate["id"])["hash"] == edited["hash"]
    await jobs.shutdown()


@pytest.mark.asyncio
async def test_text_behavior_checks_keep_limits_explicit(store):
    jobs = LearningJobs(store, lambda c: ["some training input"], lambda cfg: Model())
    candidate = store.create_candidate("方法", "global", [])
    candidate = store.edit_candidate(candidate["id"], 1, BODY)
    cases = [{"input": "new held out input " + str(i), "required": ["执行步骤"], "forbidden": ["已修改文件"]} for i in range(2)]
    job = jobs.submit(candidate["id"], candidate["revision"], "evaluate", "eval", BUDGET, cases)
    await jobs.tasks[job["id"]]
    final = store.candidate(candidate["id"])
    assert final["evaluation"] == "behavior_pass"
    assert final["behavior"]["kind"] == "text_constraints_only"
    assert store.skills.list_active() == []
    await jobs.shutdown()


@pytest.mark.asyncio
async def test_cancelled_model_cannot_apply_a_late_response_even_if_sdk_swallows_cancel(store):
    started = asyncio.Event()

    class Resistant(Model):
        async def ainvoke(self, messages):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().ainvoke(messages)

    jobs = LearningJobs(store, lambda c: [], lambda cfg: Resistant())
    candidate = store.create_candidate('方法', 'global', [])
    job = jobs.submit(candidate['id'], 1, 'generate', 'cancel-flight', BUDGET)
    task = jobs.tasks[job['id']]
    await started.wait()
    jobs.cancel(job['id'])
    await task
    assert jobs.read(job['id'])['state'] == 'cancelled'
    assert store.candidate(candidate['id'])['revision'] == 1
    await jobs.shutdown()


@pytest.mark.asyncio
async def test_shutdown_marks_queued_job_cancelled_and_explicit_off_blocks_calls(store, monkeypatch):
    jobs = LearningJobs(store, lambda c: [], lambda cfg: Model())
    candidate = store.create_candidate('方法', 'global', [])
    job = jobs.submit(candidate['id'], 1, 'generate', 'shutdown-queued', BUDGET)
    assert await jobs.shutdown()
    assert jobs.read(job['id'])['state'] == 'cancelled'
    another = LearningJobs(store, lambda c: [], lambda cfg: Model())
    monkeypatch.setenv('NOVAMIND_SKILL_EVOLUTION', 'off')
    with pytest.raises(ValueError, match='禁止'):
        another.submit(candidate['id'], 1, 'generate', 'off', BUDGET)
    await another.shutdown()
