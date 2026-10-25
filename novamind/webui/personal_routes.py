"""Personal workspace boundary: settings, images, schedules and memory commands."""
from __future__ import annotations

import asyncio
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from novamind.core import personal, schedules
from novamind.core.images import image_path, save_image, claim_images
from .api_models import QuickAck

router = APIRouter()


class Style(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["default", "concise", "detailed", "warm", "formal", "custom"] = "default"
    custom: str = Field(default="", max_length=2000)


class Desktop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bubble_visible: bool = True
    startup_bubble: bool = True
    autostart: bool = False
    open_workspace: bool = True
    size: Literal[40, 48, 56] = 48
    opacity: float = Field(default=1, ge=.35, le=1)
    theme: Literal["follow", "light", "dark"] = "follow"
    accent: Literal["blue", "slate", "green"] = "blue"
    icon: str = Field(default="brand", max_length=32, pattern=r"^(brand|spark|[a-f0-9]{32})$")
    icon_fit: Literal["contain", "cover"] = "contain"
    snap: bool = True
    motion: bool = True
    status: bool = True
    hotkey: str = Field(default="Ctrl+Shift+Space", max_length=45,
                        pattern=r"^(Ctrl\+)?(Alt\+)?(Shift\+)?(Space|[A-Z]|F(?:[1-9]|1[0-2]))$")
    bubble_x: int | None = None
    bubble_y: int | None = None

    @field_validator("hotkey")
    @classmethod
    def safe_hotkey(cls, value: str) -> str:
        key = value.split("+")[-1]
        function_key = key.startswith("F") and key[1:].isdigit()
        if not function_key and "Ctrl+" not in value and "Alt+" not in value:
            raise ValueError("字母或空格热键至少搭配 Ctrl 或 Alt，避免占用日常输入")
        return value


@router.get("/personal/settings")
def read_settings():
    return {**personal.settings(), "desktop_status": personal.desktop_status}


class Theme(BaseModel):
    theme: Literal["system", "light", "dark"]


@router.put("/personal/theme")
def write_theme(body: Theme):
    return personal.update_settings("appearance", body.model_dump())


@router.put("/personal/settings/style")
def write_style(body: Style):
    return personal.update_settings("style", body.model_dump())


@router.put("/personal/settings/desktop")
def write_desktop(body: Desktop):
    # Omitted fields are not resets; a stale settings form must not move a live ball.
    body = Desktop.model_validate({**personal.settings()["desktop"], **body.model_dump(exclude_unset=True)})
    if body.icon not in {"brand", "spark"} and not image_path(body.icon).is_file():
        raise HTTPException(400, "图标图片不存在")
    if body.icon not in {"brand", "spark"}:
        claim_images([body.icon])
    if body.autostart != personal.settings()["desktop"]["autostart"]:
        from novamind.desktop.autostart import configure
        try:
            configure(body.autostart)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, "开机自启设置失败：" + str(exc)) from None
    return personal.update_settings("desktop", body.model_dump())


@router.post("/attachments/images")
async def upload_image(request: Request):
    try:
        return await asyncio.to_thread(save_image, await request.body())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(422, "图片无法读取，请使用 PNG、JPEG 或静态 WebP") from None


@router.get("/attachments/images/{image_id}")
def read_image(image_id: str):
    try:
        path = image_path(image_id)
    except ValueError:
        raise HTTPException(404, "图片不存在") from None
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=2000)
    target_time: str = ""
    task_date: str | None = None
    repeat: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    repeat_count: int | None = Field(default=None, ge=1)
    remind_minutes: int = Field(default=0, ge=0, le=525600)
    source_id: str = Field(default="", max_length=128)


class ScheduleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_time: str | None = None
    task_date: str | None = None
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    status: Literal["pending", "completed", "cancelled"] | None = None
    remind_minutes: int | None = Field(default=None, ge=0, le=525600)


@router.get("/personal/schedules")
def read_schedules():
    return {"tasks": schedules.list_tasks()}


@router.post("/personal/schedules")
def add_schedule(body: Schedule):
    try:
        return schedules.create(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.patch("/personal/schedules/{task_id}")
def edit_schedule(task_id: str, body: ScheduleChange):
    try:
        return schedules.change(task_id, body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/personal/schedules/{task_id}/read")
def acknowledge(task_id: str):
    schedules.acknowledge(task_id)
    return {"status": "ok"}


@router.delete("/personal/schedules/{task_id}", response_model=QuickAck)
def delete_schedule(task_id: str):
    schedules.delete(task_id)
    return {"status": "ok"}


class NewMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)
    category: Literal["preference", "project_context", "experience", "active_task"] = "preference"
    scope: str = Field(default="global", min_length=1, max_length=120)


class MemoryDocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=32000)
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    scope: str = Field(default="global", min_length=1, max_length=120)


class MemoryDocumentResponse(BaseModel):
    content: str
    revision: str
    path: str
    scope: str
    conflict: bool = False
    server_content: str | None = None


@router.get("/personal/memory-document", response_model=MemoryDocumentResponse, response_model_exclude_none=True)
def read_memory_document(scope: str = "global"):
    from novamind.core.memory.document import read_document
    from novamind.core.memory.governance_runtime import get_governance_service
    if not scope or len(scope) > 120:
        raise HTTPException(422, "记忆范围须为 1–120 字符")
    from novamind.core.memory.governance import GovernanceError, GovernanceConflictError
    try:
        return read_document(get_governance_service(), scope)
    except GovernanceConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except GovernanceError as exc:
        raise HTTPException(422, str(exc)) from None


@router.put("/personal/memory-document", response_model=MemoryDocumentResponse, response_model_exclude_none=True)
def write_memory_document(body: MemoryDocumentUpdate):
    from novamind.core.memory.document import write_document
    from novamind.core.memory.governance_runtime import get_governance_service
    from novamind.core.memory.governance import GovernanceError, GovernanceConflictError
    try:
        return write_document(get_governance_service(), body.content, body.revision, body.scope)
    except GovernanceConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except GovernanceError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/personal/memories")
def add_memory(body: NewMemory):
    from novamind.core.memory.governance_runtime import get_governance_service
    record = get_governance_service().save_explicit(body.content, category=body.category, scope=body.scope)
    return {"memory_id": record.memory_id, "state": record.state.value}


@router.get("/personal/memories/export")
def export_memory():
    from novamind.core.memory.governance_runtime import get_governance_service
    from novamind.core.memory.governance import MemoryState
    service = get_governance_service()
    return {"memories": [{**r.__dict__, "content": service.content_for(r.memory_id)}
                         for r in service.list_governed() if r.state != MemoryState.DELETED]}


@router.post("/personal/memories/{memory_id}/purge")
def purge_memory(memory_id: str):
    from .server import get_runtime
    get_runtime().get_learning().store.revoke_memory(memory_id)
    from pathlib import Path
    from novamind.core import config
    from novamind.core.memory.governance_runtime import get_governance_service
    service = get_governance_service()
    with service.store._lock:
        trace = service.trace_store.get(memory_id)
        service.delete(memory_id)
        if trace:
            content = trace.content
            service.trace_store.remove(memory_id)
            # Existing profile files remain migration backups only. Remove the
            # selected body there too, without touching unrelated user content.
            paths = [Path(config.PROFILE_PATH)] + list(Path(config.PROFILE_BACKUP_DIR).glob("user_profile.*.md"))
            paths += list(Path(config.MEMORY_DIR).glob("MEMORY*.md"))
            import json
            backup_root = Path(config.WORKSPACE_DIR) / "backups"
            for folder in backup_root.glob("personal-v1-*"):
                manifest = folder / "manifest.json"
                if manifest.is_file() and folder.resolve().is_relative_to(backup_root.resolve()):
                    for item in json.loads(manifest.read_text(encoding="utf-8")):
                        path = folder / item["backup"]
                        if item.get("memory_body") and path.resolve().is_relative_to(folder.resolve()):
                            paths.append(path)
            for path in paths:
                if path.is_file():
                    old = path.read_text(encoding="utf-8")
                    variants = {content, content.replace("\n", "\n  ")}
                    if content and any(value in old for value in variants):
                        for value in sorted(variants, key=len, reverse=True):
                            old = old.replace(value, "[已清除]")
                        temporary = path.with_suffix(path.suffix + ".purge-tmp")
                        temporary.write_text(old, encoding="utf-8")
                        temporary.replace(path)
    return {"status": "ok", "scope": "记忆正文、索引、旧画像和升级快照中的正文；聊天历史未删除"}
