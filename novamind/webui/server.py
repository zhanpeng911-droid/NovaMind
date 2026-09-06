"""NovaMind WebUI 后端（FastAPI + SSE 流式）。

复用 create_agent_app + agent.astream，把 Agent 的异步流式输出以 SSE 推给前端。

加固 Phase 5：
- WebRuntime（lifespan 持有）替代模块级单例与全局 chat 锁；
- SSE 语义：断连即取消本轮（显式 re-raise CancelledError，不向死连接写
  done）；普通运行错误发 error + done；done 只在正常完成时发；
- API 边界：请求体大小限制（同时校验 Content-Length 与实际字节）、
  /chat 只接受 JSON、统一错误形状、response_model 声明；
- 分页：sessions/history/monitor/skills 走稳定游标（带版本 base64url）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

from novamind.core.config import LOG_DIR
from novamind.core.state_machine import ConversationStore
from novamind.webui.api_models import (
    DeleteResponse,
    DoctorResponse,
    ErrorResponse,
    HealthResponse,
    HistoryResponse,
    InvalidCursorError,
    MonitorEventsResponse,
    MonitorSessionsResponse,
    PaginationMeta,
    SessionsResponse,
    SkillsResponse,
    decode_cursor,
    decode_monitor_events_cursor,
    encode_cursor,
    encode_monitor_events_cursor,
)
from novamind.webui.runtime import WebRuntime

logger = logging.getLogger("novamind.webui")

STATIC_DIR = Path(__file__).parent / "static"

# 请求体上限（默认 1MB，可经环境覆盖）
_BODY_LIMIT = int(os.getenv("NOVAMIND_WEB_BODY_LIMIT", str(1024 * 1024)) or 1048576)


def _load_env() -> tuple[str, str]:
    """从 .env 读 provider/model（与 CLI 一致）。"""
    load_dotenv(_resolve_env_path())
    provider = os.getenv("DEFAULT_PROVIDER", "openai")
    model = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
    return provider, model


def _resolve_env_path() -> Path:
    """按优先级定位 .env，兼容开发模式与打包后的 exe。

    优先级：NOVAMIND_ENV 显式指定 → exe 同目录 → exe 上一级目录（项目根）
            → 源码根（开发）→ ~/.novamind/.env → 当前工作目录。
    """
    import sys

    explicit = os.getenv("NOVAMIND_ENV")
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return p

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # 1) exe 同目录（dist/.env）
        p = exe_dir / ".env"
        if p.exists():
            return p
        # 2) exe 上一级目录（项目根/.env），与数据根一致
        p = exe_dir.parent / ".env"
        if p.exists():
            return p

    src_root = Path(__file__).resolve().parents[3]
    p = src_root / ".env"
    if p.exists():
        return p

    home_env = Path.home() / ".novamind" / ".env"
    if home_env.exists():
        return home_env

    return Path.cwd() / ".env"


# ── WebRuntime 单例（lifespan 创建；模块级访问器保持测试兼容） ────────────

_runtime: WebRuntime | None = None
# 可选 runtime 工厂（压测/测试注入固定模型与调用保护；None 时用默认）
_runtime_factory: Any = None


def get_runtime() -> WebRuntime:
    """返回当前运行时；lifespan 之外（如单元测试直调）按需补建。"""
    global _runtime
    if _runtime is None:
        _runtime = _build_runtime()
    return _runtime


def _build_runtime() -> WebRuntime:
    if _runtime_factory is not None:
        return _runtime_factory()
    return WebRuntime()


def get_agent() -> Any:
    """单例 agent 访问器（兼容旧调用点；实际持有者在 WebRuntime）。"""
    return get_runtime().get_agent()


def get_history_store() -> ConversationStore:
    return get_runtime().get_history_store()


def get_skill_store() -> Any:
    return get_runtime().get_skill_store()


def _safe_id(thread_id: str) -> str:
    """thread_id → 日志文件名（与 logger 的 safe_id 逻辑一致）。"""
    return "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"


def _content_str(content: Any) -> str:
    """把 langchain content（str 或 content blocks）归一化为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_call":
                    parts.append(f"[tool_call:{block.get('name', '')}]")
        return "".join(parts)
    return str(content)


def _sse(payload: dict) -> str:
    """把 dict 序列化为一条 SSE data 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


class _BodyTooLargeError(Exception):
    """实际接收字节超过上限（覆盖 chunked / 伪造 Content-Length）。"""


class BodyLimitMiddleware:
    """纯 ASGI 中间件：请求体大小限制 + /chat JSON-only。

    - 先查 Content-Length 头（快速拒绝）；
    - 再包装 receive 统计实际累计字节，覆盖 chunked 与伪造头；
    - 超限/类型不符返回统一错误形状（413/415）。
    """

    def __init__(self, app: Any, limit: int = _BODY_LIMIT):
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        path = scope.get("path", "")

        if path == "/chat":
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                await self._send_error(send, 415, "unsupported_media_type",
                                       "/chat 只接受 JSON 请求体")
                return

        content_length = headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > self.limit:
            await self._send_error(send, 413, "body_too_large",
                                   f"请求体超过 {self.limit} 字节上限")
            return

        state = {"size": 0}

        async def sized_receive():
            msg = await receive()
            if msg.get("type") == "http.request":
                state["size"] += len(msg.get("body", b"") or b"")
                if state["size"] > self.limit:
                    raise _BodyTooLargeError()
            return msg

        try:
            await self.app(scope, sized_receive, send)
        except _BodyTooLargeError:
            await self._send_error(send, 413, "body_too_large",
                                   f"请求体超过 {self.limit} 字节上限")

    async def _send_error(self, send: Any, status: int, code: str, message: str) -> None:
        body = json.dumps({
            "error": {
                "code": code,
                "message": message,
                "request_id": uuid.uuid4().hex,
            }
        }, ensure_ascii=False).encode("utf-8")
        try:
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
        except Exception:  # 响应已开始时无法再发错误页，安全忽略
            pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _runtime
    _runtime = _build_runtime()
    app.state.runtime = _runtime
    try:
        yield
    finally:
        await _runtime.shutdown()
        _runtime = None


app = FastAPI(title="NovaMind WebUI", lifespan=_lifespan)
app.add_middleware(BodyLimitMiddleware, limit=_BODY_LIMIT)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """HTTPException → 统一错误形状（HTTPException(status, detail) 的 detail
    是服务端可控文案，不含原始异常）。"""
    codes = {
        400: "bad_request", 404: "not_found", 405: "method_not_allowed",
        413: "body_too_large", 415: "unsupported_media_type",
        422: "validation_error",
    }
    return _error_response(exc.status_code,
                           codes.get(exc.status_code, "http_error"),
                           str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """422：字段验证失败，统一错误形状（FastAPI 默认 detail 形状弃用）。"""
    return _error_response(422, "validation_error", f"字段验证失败: {exc.errors()[:1]}")


@app.exception_handler(InvalidCursorError)
async def invalid_cursor_handler(request: Request, exc: InvalidCursorError):
    return _error_response(400, "invalid_cursor", str(exc))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """500：服务端日志保留 traceback，客户端只拿稳定 code/message/request_id。"""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return _error_response(500, "internal_error", "服务器内部错误")


_ERROR_DESCRIPTIONS = {
    400: "非法游标（invalid_cursor）",
    413: "请求体过大",
    415: "媒体类型不支持",
    422: "字段验证失败",
    500: "服务器内部错误",
}


def _error_responses(*codes: int) -> dict[int, dict]:
    """给路由的 OpenAPI 声明补统一错误模型。"""
    return {c: {"model": ErrorResponse, "description": _ERROR_DESCRIPTIONS[c]}
            for c in codes}


def _error_response(status: int, code: str, message: str,
                    request_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message,
                           "request_id": request_id or uuid.uuid4().hex}},
    )


@app.post("/chat", responses={
    200: {"content": {"text/event-stream": {}},
          "description": "SSE 事件流（thread/tool/text/limit/error/done）"},
    **_error_responses(413, 415, 422, 500),
})
async def chat(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_chat(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_chat(request: ChatRequest) -> AsyncIterator[str]:
    runtime = get_runtime()
    thread_id = request.thread_id or f"gui_{uuid.uuid4().hex[:12]}"
    yield _sse({"type": "thread", "thread_id": thread_id})

    task = asyncio.current_task()
    runtime.register_task(task)
    gen = None
    stream_error: dict | None = None
    try:
        # Phase 5：移除全局 chat 锁——先拿容量许可，正确性由 Agent 的
        # per-thread 协调器保证（同 thread 串行，跨 thread 并发）。
        async with runtime.capacity:
            agent_or_coro = get_agent()
            agent = (
                await agent_or_coro if asyncio.iscoroutine(agent_or_coro)
                else agent_or_coro
            )
            gen = agent.astream(request.message, thread_id=thread_id)
            async for event in gen:
                for node_name, node_data in event.items():
                    if node_name == "agent":
                        messages = node_data.get("messages") or []
                        last = messages[-1] if messages else None
                        if last is None:
                            continue
                        tool_calls = getattr(last, "tool_calls", None)
                        if tool_calls:
                            for tc in tool_calls:
                                name = (
                                    tc.get("name")
                                    if isinstance(tc, dict)
                                    else getattr(tc, "name", "?")
                                )
                                yield _sse({"type": "tool", "name": name})
                        else:
                            content = _content_str(getattr(last, "content", ""))
                            if content:
                                yield _sse({"type": "text", "content": content})
                    elif node_name == "__limit__":
                        yield _sse({"type": "limit"})
    except asyncio.CancelledError:
        # 断连/停机：显式处理并 re-raise；finally 的 aclose 会取消本轮并
        # 回滚半轮状态，不向已死连接写 done。
        raise
    except Exception:
        request_id = uuid.uuid4().hex
        logger.exception("chat stream failed (request_id=%s)", request_id)
        # 只发稳定信息，不泄露 provider/路径等内部细节（完整 traceback 留在服务端日志）
        stream_error = {
            "type": "error", "code": "internal_error",
            "message": "服务器处理失败，请稍后重试",
            "request_id": request_id,
        }
    finally:
        # 持有下层 async generator 并在退出时关闭（幂等）：
        # 提前断连时触发半轮回滚与沙箱释放
        if gen is not None:
            with contextlib.suppress(Exception):
                await gen.aclose()
        runtime.unregister_task(task)

    # 只有正常路径到达这里（取消/断连已在上方 re-raise）。
    # done 只在正常完成时发：普通运行错误先发 error 再发 done。
    if stream_error is not None:
        yield _sse(stream_error)
    yield _sse({"type": "done"})


@app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/doctor", response_model=DoctorResponse)
async def doctor():
    """架构健康体检：复用 novamind doctor 的分层自检，返回结构化报告。"""
    try:
        from novamind.core.doctor import run_doctor

        return run_doctor().as_dict()
    except Exception:
        logger.exception("doctor failed")
        return {
            "ok": False,
            "counts": {"error": 1, "warning": 0, "info": 0},
            "findings": [{
                "level": "error",
                "code": "doctor_failed",
                "message": "诊断执行失败，请查看服务端日志",
                "suggestion": "Check the server log for the full traceback.",
            }],
        }


def _pagination(limit: int, page: list, next_cursor: str | None) -> PaginationMeta:
    return PaginationMeta(
        limit=limit, count=len(page),
        has_more=next_cursor is not None, next_cursor=next_cursor,
    )


@app.get("/monitor/sessions", response_model=MonitorSessionsResponse,
         response_model_exclude_none=True, responses=_error_responses(400, 500))
async def monitor_sessions(limit: int | None = None, cursor: str | None = None):
    limit = max(1, min(int(limit), 500)) if limit is not None else None
    """列出所有可监控的会话日志（logs/*.jsonl），支持稳定分页。

    游标按 (mtime_ns, filename) 逆序；无 limit 时返回全量（兼容旧行为）。"""
    if not os.path.isdir(LOG_DIR):
        return MonitorSessionsResponse(sessions=[])

    entries: list[dict] = []
    for fname in os.listdir(LOG_DIR):
        if not fname.endswith(".jsonl"):
            continue
        path = os.path.join(LOG_DIR, fname)
        if not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        entries.append({
            "thread_id": fname[:-6],
            "size_bytes": st.st_size,
            "last_modified": st.st_mtime,
            "mtime_ns": st.st_mtime_ns,
        })
    # (mtime_ns, filename) 逆序：相同 mtime 下顺序稳定
    entries.sort(key=lambda s: (s["mtime_ns"], s["thread_id"]), reverse=True)

    if cursor:
        data = decode_cursor(cursor, "monitor_sessions",
                             {"mtime_ns": int, "thread_id": str})
        pos = (data["mtime_ns"], data["thread_id"])
        entries = [e for e in entries if (e["mtime_ns"], e["thread_id"]) < pos]

    next_raw = None
    if limit is not None:
        page = entries[:limit]
        has_more = len(entries) > limit
        if has_more and page:
            last = page[-1]
            next_raw = encode_cursor("monitor_sessions", {
                "mtime_ns": last["mtime_ns"], "thread_id": last["thread_id"],
            })
        return MonitorSessionsResponse(
            sessions=page, pagination=_pagination(limit, page, next_raw)
        )

    return MonitorSessionsResponse(sessions=entries)


@app.get("/monitor/events/{thread_id}", response_model=MonitorEventsResponse,
         response_model_exclude_none=True, responses=_error_responses(400, 422, 500))
async def monitor_events(thread_id: str, limit: int | None = None,
                         cursor: str | None = None):
    """返回指定会话的审计事件流（解析后的 JSON 列表）。

    收尾修复 Phase 3 语义（tail-follow，有界读取）：
    - 默认 limit=200，上限 1000；非法 limit → 422；省略 limit 不再返回全量；
    - 无 cursor：从文件尾读最近 limit 条完整行；next_cursor 指向尾行起点
      （EOF 也返回，空页也返回），供后续增量读取；
    - 有 cursor：只读其后新增行（有界扫描 ≤2 MiB），EOF 时 has_more=false
      但 next_cursor 仍保留当前位置，追加日志后可继续查更新；
    - 文件截断导致 offset 越界 → 400 invalid_cursor，前端应清空旧游标
      刷新第一页；
    - 单行超过 256 KiB / 坏 JSON / 非对象 JSON 跳过并记录可控日志。"""
    if limit is None:
        limit = 200
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422,
                                detail="limit 必须是整数") from None
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=422,
                                detail="limit 必须在 1~1000 之间") from None

    path = os.path.join(LOG_DIR, f"{_safe_id(thread_id)}.jsonl")
    if not os.path.isfile(path):
        return MonitorEventsResponse(events=[])

    offset: int | None = None
    discarding = False
    if cursor:
        try:
            offset, discarding = decode_monitor_events_cursor(cursor)
        except InvalidCursorError:
            raise

    from novamind.webui.event_reader import (
        OffsetBeyondEOFError,
        read_events,
    )

    try:
        result = await asyncio.to_thread(
            read_events, path, limit=limit,
            offset=offset, discarding=discarding,
        )
    except OffsetBeyondEOFError:
        # 文件截断/轮转：游标越界
        raise InvalidCursorError("monitor file truncated below cursor offset") from None

    next_raw = encode_monitor_events_cursor(result.next_offset, result.discarding)
    return MonitorEventsResponse(
        events=result.events,
        pagination=PaginationMeta(
            limit=limit, count=len(result.events),
            has_more=result.has_more, next_cursor=next_raw,
        ),
    )


@app.get("/skills", response_model=SkillsResponse,
         response_model_exclude_none=True, responses=_error_responses(400, 500))
async def list_skills(limit: int | None = None, cursor: str | None = None):
    limit = max(1, min(int(limit), 500)) if limit is not None else None
    """列出所有激活技能（名称/描述/四计数器/effective_rate），支持稳定分页。

    store 层按 (lower(name), skill_id) 排序；count 始终表示全部 active 数。"""
    try:
        store = get_skill_store()
        total = await asyncio.to_thread(store.count_active)
        if limit is not None:
            cur = None
            if cursor:
                data = decode_cursor(cursor, "skills",
                                     {"name": str, "skill_id": str})
                cur = (data["name"], data["skill_id"])
            recs, next_cur = await asyncio.to_thread(
                store.list_active_page, limit, cur
            )
            next_raw = (
                encode_cursor("skills", {"name": next_cur[0], "skill_id": next_cur[1]})
                if next_cur else None
            )
            pagination = _pagination(limit, recs, next_raw)
        else:
            recs = await asyncio.to_thread(store.list_active)
            pagination = None
    except InvalidCursorError:
        raise
    except Exception:
        logger.exception("list skills failed")
        raise HTTPException(status_code=500,
                            detail="技能库暂不可用，请稍后重试") from None

    skills = [{
        "name": r.name,
        "description": r.description,
        "selections": r.total_selections,
        "applied": r.total_applied,
        "completions": r.total_completions,
        "fallbacks": r.total_fallbacks,
        "effective_rate": round(r.effective_rate, 3),
        "enabled": r.enabled,
        "is_active": r.is_active,
    } for r in recs]
    return SkillsResponse(skills=skills, count=total, pagination=pagination)


@app.get("/sessions", response_model=SessionsResponse,
         response_model_exclude_none=True, responses=_error_responses(400, 500))
async def list_sessions(limit: int | None = None, cursor: str | None = None):
    limit = max(1, min(int(limit), 500)) if limit is not None else None
    """列出所有会话（供前端侧边栏），支持稳定分页。

    Phase 4/5：走 store 的稳定分页 API（一条 JOIN/页，无 N+1）；无 limit
    时循环取完所有页（兼容旧行为），响应形状保持 {"sessions": [...]}。"""
    try:
        store = get_history_store()
        if limit is not None:
            cur = None
            if cursor:
                data = decode_cursor(cursor, "sessions",
                                     {"last_id": int, "thread_id": str})
                cur = (data["last_id"], data["thread_id"])
            page, next_cur = await asyncio.to_thread(
                store.list_thread_page, limit, cur
            )
            next_raw = (
                encode_cursor("sessions", {
                    "last_id": next_cur[0], "thread_id": next_cur[1],
                }) if next_cur else None
            )
            return SessionsResponse(
                sessions=page, pagination=_pagination(limit, page, next_raw)
            )
        items: list[dict] = []
        cur = None
        while True:
            page, cur = await asyncio.to_thread(
                store.list_thread_page, 100, cur
            )
            items.extend(page)
            if cur is None:
                break
        return SessionsResponse(sessions=items)
    except InvalidCursorError:
        raise
    # 存储故障不再伪装 200 空数组：向上传播由全局 handler 返回 500


@app.get("/history/{thread_id}", response_model=HistoryResponse,
         response_model_exclude_none=True, responses=_error_responses(400, 500))
async def get_history(thread_id: str, limit: int | None = None,
                      cursor: str | None = None):
    limit = max(1, min(int(limit), 1000)) if limit is not None else None
    """返回某会话的消息历史（供前端切换会话时加载），支持稳定分页。

    无 limit 时全量加载（兼容旧行为）；有 limit 时走 load_message_page，
    游标为数据库原始行 id（含 tool 行），tool-only 页也能推进。"""
    try:
        store = get_history_store()
        if limit is not None:
            before = None
            if cursor:
                data = decode_cursor(cursor, "history", {"before_id": int})
                before = data["before_id"]
            msgs, next_cur = await asyncio.to_thread(
                store.load_message_page, thread_id, limit, before
            )
            next_raw = (
                encode_cursor("history", {"before_id": next_cur})
                if next_cur is not None else None
            )
            pagination = _pagination(limit, msgs, next_raw)
        else:
            msgs = await asyncio.to_thread(store.load_messages, thread_id)
            pagination = None
    except InvalidCursorError:
        raise
    # 存储故障不再伪装 200 空数组：向上传播由全局 handler 返回 500
    # （未知 thread 本身不抛错，仍返回 200 空列表）

    items: list[dict] = []
    for m in msgs:
        role = getattr(m, "type", "")
        if role == "tool":
            continue  # 工具消息不展示，工具调用以标签形式挂在 AI 消息上
        content = m.content if isinstance(m.content, str) else _content_str(m.content)
        tools: list[str] = []
        for tc in (getattr(m, "tool_calls", None) or []):
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name:
                tools.append(name)
        items.append({
            "role": "user" if role == "human" else "ai",
            "content": content,
            "tools": tools,
        })
    return HistoryResponse(messages=items, pagination=pagination)


@app.delete("/sessions/{thread_id}", response_model=DeleteResponse,
            response_model_exclude_none=True, responses=_error_responses(500))
async def delete_session(thread_id: str):
    """删除指定会话（前端侧边栏删除按钮）。

    成功与删除不存在的 thread 幂等返回 200 {"status":"ok"}；实际删除失败
    返回结构化 500（code=session_delete_failed，同一 request_id 写日志并
    返回），不转成通用 http_error。"""
    try:
        # Agent 的按 thread 互斥删除（等待在飞轮次结束）；aclear 先删数据库
        # 成功后才清内存，失败时内存与数据库都保留旧会话。
        runtime = get_runtime()
        agent = runtime.agent_if_ready()
        if agent is not None:
            await agent.aclear_conversation(thread_id)
        else:
            await asyncio.to_thread(runtime.get_history_store().clear_thread, thread_id)
        return DeleteResponse(status="ok")
    except Exception:
        request_id = uuid.uuid4().hex
        logger.exception("delete session failed (request_id=%s)", request_id)
        return _error_response(500, "session_delete_failed",
                               "删除会话失败，请稍后重试", request_id)


# 静态文件（前端 index.html）最后挂载，避免拦截 /chat /health
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _custom_openapi() -> dict:
    """修正 FastAPI 自动生成的 OpenAPI：

    - /chat 的 200 只保留 text/event-stream（自动生成会把
      application/json 与 SSE 并列）；
    - 其余由路由 responses= 声明补齐错误模型。"""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title=app.title, version=app.version,
                         description=app.description, routes=app.routes)
    chat_op = schema.get("paths", {}).get("/chat", {}).get("post")
    if chat_op and "200" in chat_op.get("responses", {}):
        sse_frame_hint = "SSE frame: data: {json}" + chr(92) + "n" + chr(92) + "n"
        chat_op["responses"]["200"]["content"] = {
            "text/event-stream": {
                "schema": {"type": "string", "description": sse_frame_hint},
            },
        }
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]
