"""Local, reviewed skill learning. Reading an endpoint never calls a model."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from novamind.core import personal
from novamind.core.skill.learning.policy import LearningError


class LearningRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except LearningError as error:
                raise HTTPException(error.status, str(error)) from None
        return handler


router = APIRouter(prefix="/personal/learning", route_class=LearningRoute)


def service():
    from .server import get_runtime
    return get_runtime().get_learning()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Settings(Strict):
    enabled: bool | None = None
    min_samples: int | None = Field(default=None, ge=3, le=20)
    retention_days: int | None = Field(default=None, ge=1, le=90)
    max_observations: int | None = Field(default=None, ge=100, le=5000)
    cooldown_days: int | None = Field(default=None, ge=1, le=30)


class Revision(Strict):
    revision: int = Field(ge=1)


class Candidate(Strict):
    title: str = Field(min_length=1, max_length=80)
    scope: str = Field(default="global", min_length=1, max_length=120)
    sources: list[str] = Field(default_factory=list, max_length=10)
    baseline_id: str | None = Field(default=None, max_length=100)
    global_confirmed: bool = False


class Edit(Revision):
    body: str = Field(max_length=16000)


class Publish(Revision):
    hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_base_id: str | None = None
    mode: Literal["trial", "verified"]
    enable: bool = False
    confirmed: Literal[True]


class Budget(Strict):
    budget_yuan: float = Field(gt=0, le=10000)
    input_price: float = Field(gt=0, le=10000)
    output_price: float = Field(gt=0, le=10000)
    provider: str = Field(max_length=40)
    model: str = Field(min_length=1, max_length=100)


class Case(Strict):
    input: str = Field(min_length=1, max_length=4000)
    required: list[str] = Field(min_length=1, max_length=10)
    forbidden: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("required", "forbidden")
    @classmethod
    def meaningful_constraints(cls, values):
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("每条文本约束须为 1–300 个有效字符")
        return values


class Job(Revision):
    idempotency_key: str = Field(min_length=1, max_length=100)
    budget: Budget
    cases: list[Case] = Field(default_factory=list, max_length=5)


class Feedback(Strict):
    run_id: str = Field(min_length=1, max_length=100)
    skill_id: str = Field(min_length=1, max_length=100)
    value: Literal["helpful", "problem", "unknown"]
    revision: int = Field(ge=1)


@router.get("/settings")
async def settings():
    import os
    return {**personal.settings()["learning"], "provider": os.getenv("DEFAULT_PROVIDER", "openai"),
            "model": os.getenv("DEFAULT_MODEL", ""), "paid_calls_automatic": False}


@router.put("/settings")
async def update_settings(body: Settings):
    result = personal.update_settings("learning", body.model_dump(exclude_none=True))
    service().observer.refresh()
    return result


@router.get("/patterns")
async def patterns():
    return {"patterns": service().store.patterns(personal.settings()["learning"]["min_samples"])}


@router.get("/patterns/{key}/sources")
async def sources(key: str):
    return {"sources": service().source_preview(key)}


@router.post("/patterns/{key}/dismiss")
async def dismiss(key: str, body: Revision):
    return service().store.dismiss(key, body.revision)


@router.get("/candidates")
async def candidates(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    service().refresh_sources()
    return {"candidates": service().store.items("candidates", limit=limit, offset=offset)}


@router.post("/candidates")
async def create(body: Candidate):
    return service().store.create_candidate(body.title, body.scope, body.sources, baseline_id=body.baseline_id,
                                            global_confirmed=body.global_confirmed)


@router.get("/candidates/{key}")
async def candidate(key: str):
    service().refresh_sources()
    item = service().store.candidate(key)
    return {**item, "source_preview": service().sources(item) if item["state"] not in {"revoked", "rejected"} else []}


@router.patch("/candidates/{key}")
async def edit(key: str, body: Edit):
    return service().store.edit_candidate(key, body.revision, body.body)


@router.post("/candidates/{key}/publish")
async def publish(key: str, body: Publish):
    return service().publisher.publish(key, body.revision, body.hash, body.expected_base_id, body.mode, body.enable,
                                       personal.settings()["learning"]["cooldown_days"])


@router.post("/candidates/{key}/reject")
async def reject(key: str, body: Revision):
    return service().store.reject(key, body.revision)


@router.post("/candidates/{key}/generate")
async def generate(key: str, body: Job):
    return service().jobs.submit(key, body.revision, "generate", body.idempotency_key, body.budget.model_dump())


@router.post("/candidates/{key}/evaluate")
async def evaluate(key: str, body: Job):
    return service().jobs.submit(key, body.revision, "evaluate", body.idempotency_key, body.budget.model_dump(),
                                 [case.model_dump() for case in body.cases])


@router.get("/jobs")
async def jobs():
    return {"jobs": service().store.items("jobs", limit=20)}


@router.get("/jobs/{key}")
async def job(key: str):
    return service().jobs.read(key)


@router.post("/jobs/{key}/cancel")
async def cancel(key: str):
    return service().jobs.cancel(key)


@router.post("/jobs/{key}/acknowledge-risk")
async def acknowledge(key: str, confirmed: bool = Query(...)):
    if not confirmed:
        raise HTTPException(400, "请先明确确认已核对费用")
    return service().acknowledge_risk(key)


@router.get("/usage")
async def usage(thread_id: str = Query(max_length=200)):
    return service().usage(thread_id)


@router.post("/feedback")
async def feedback(body: Feedback):
    return service().feedback(body.run_id, body.skill_id, body.value, body.revision)


@router.post("/skills/{key}/rollback")
async def rollback(key: str, expected_active: str = Query(max_length=100)):
    return service().publisher.rollback(key, expected_active)


@router.delete("/skills/{key}")
async def revoke(key: str):
    return service().publisher.revoke(key)


@router.delete("/observations")
async def clear(confirmed: bool = Query(...)):
    if not confirmed:
        raise HTTPException(400, "清理观察需要明确确认")
    return service().clear_observations()
