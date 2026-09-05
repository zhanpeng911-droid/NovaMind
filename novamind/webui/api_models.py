"""NovaMind Web API 契约模型（加固 Phase 5）。

统一请求/成功响应/分页元数据/错误形状：
- ErrorResponse：客户端只看到稳定 code/message/request_id，traceback 留在服务端日志；
- PaginationMeta：limit/count/has_more/next_cursor；
- 游标：带版本的 base64url JSON，严格校验类型与边界——非法游标由端点映射为 400。
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from pydantic import BaseModel, Field

_CURSOR_VERSION = "v1"


class ErrorBody(BaseModel):
    code: str = Field(examples=["invalid_cursor"])
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    """统一错误形状：所有 4xx/5xx 响应的 error 字段结构一致。"""

    error: ErrorBody


class PaginationMeta(BaseModel):
    limit: int
    count: int
    has_more: bool
    next_cursor: str | None = None


# ── 成功响应模型（路由以 response_model 声明；None 字段被 exclude_none 排除，
#    不改变既有 JSON 形状）───────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str


class SessionItem(BaseModel):
    thread_id: str
    title: str
    message_count: int
    last_ts: str


class SessionsResponse(BaseModel):
    sessions: list[SessionItem]
    pagination: PaginationMeta | None = None


class HistoryItem(BaseModel):
    role: str
    content: str
    tools: list[str]


class HistoryResponse(BaseModel):
    messages: list[HistoryItem]
    pagination: PaginationMeta | None = None


class DeleteResponse(BaseModel):
    status: str
    message: str | None = None


class SkillItem(BaseModel):
    name: str
    description: str
    selections: int
    applied: int
    completions: int
    fallbacks: int
    effective_rate: float
    enabled: bool
    is_active: bool


class SkillsResponse(BaseModel):
    skills: list[SkillItem]
    count: int
    pagination: PaginationMeta | None = None
    # store 故障时的兼容字段（契约：错误 payload 而非 500）
    error: str | None = None


class MonitorSessionItem(BaseModel):
    thread_id: str
    size_bytes: int
    last_modified: float
    mtime_ns: int


class MonitorSessionsResponse(BaseModel):
    sessions: list[MonitorSessionItem]
    pagination: PaginationMeta | None = None


class MonitorEventsResponse(BaseModel):
    events: list[dict[str, Any]]
    pagination: PaginationMeta | None = None


# ── 游标编码 ─────────────────────────────────────────────────────────


class InvalidCursorError(ValueError):
    """游标非法（版本不符/类型错误/越界）→ 端点映射为 400 invalid_cursor。"""


def encode_cursor(kind: str, data: dict[str, Any]) -> str:
    """编码带版本的 base64url 游标。kind 用于防止跨端点复用游标。"""
    payload = {"v": _CURSOR_VERSION, "k": kind, "d": data}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(raw: str, kind: str, fields: dict[str, type]) -> dict[str, Any]:
    """解码并严格校验游标：版本、kind、字段名与类型都必须精确匹配。

    fields: {字段名: 期望类型}。期望类型只支持 int/str；额外字段视为非法。
    校验失败抛 InvalidCursorError。"""
    if not raw or len(raw) > 512:
        raise InvalidCursorError("cursor missing or too long")
    try:
        blob = base64.urlsafe_b64decode(raw.encode("ascii"))
        payload = json.loads(blob.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise InvalidCursorError("cursor is not valid base64url JSON") from None
    if not isinstance(payload, dict):
        raise InvalidCursorError("cursor payload must be an object")
    if payload.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError("unsupported cursor version")
    if payload.get("k") != kind:
        raise InvalidCursorError("cursor kind mismatch")
    data = payload.get("d")
    if not isinstance(data, dict) or set(data.keys()) != set(fields.keys()):
        raise InvalidCursorError("cursor fields mismatch")
    for name, expected in fields.items():
        value = data[name]
        if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise InvalidCursorError(f"cursor field '{name}' must be int")
        if expected is str and not isinstance(value, str):
            raise InvalidCursorError(f"cursor field '{name}' must be str")
    return data
