"""NovaMind WebUI 后端（FastAPI + SSE 流式）。

复用 create_agent_app + agent.astream，把 Agent 的异步流式输出以 SSE 推给前端。
单例 agent（懒加载，首次 /chat 时创建）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from novamind.core.agent import create_agent_app
from novamind.core.provider import get_provider
from novamind.core.middlewares.default_stack import build_default_middlewares
from novamind.core.config import DB_PATH, LOG_DIR, SKILL_DB_PATH
from novamind.core.state_machine import ConversationStore

logger = logging.getLogger("novamind.webui")

STATIC_DIR = Path(__file__).parent / "static"

_agent: Any = None
_history_store: ConversationStore | None = None
_skill_store: Any = None
_chat_lock = asyncio.Lock()


def _safe_id(thread_id: str) -> str:
    """thread_id → 日志文件名（与 logger 的 safe_id 逻辑一致）。"""
    return "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"


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


def _load_env() -> tuple[str, str]:
    """从 .env 读 provider/model（与 CLI 一致）。"""
    load_dotenv(_resolve_env_path())
    provider = os.getenv("DEFAULT_PROVIDER", "openai")
    model = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
    return provider, model


def get_agent() -> Any:
    """单例 agent（懒加载）。"""
    global _agent
    if _agent is None:
        provider, model = _load_env()
        # 缺陷#2 修复：默认 GUI 后端也挂载记忆/治理中间件
        llm = get_provider(provider_name=provider, model_name=model)
        _agent = create_agent_app(provider_name=provider, model_name=model,
                                  middlewares=build_default_middlewares(llm))
    return _agent


def get_history_store() -> ConversationStore:
    """独立的历史读取 store（只读，与 agent 的写 store 共享同一 SQLite 文件）。"""
    global _history_store
    if _history_store is None:
        _history_store = ConversationStore(db_path=DB_PATH)
    return _history_store


def get_skill_store() -> Any:
    """单例技能 store（懒加载，首次访问时 discover 内置技能）。"""
    global _skill_store
    if _skill_store is None:
        from novamind.core.skill import SQLiteSkillStore
        import novamind.core.skill as skill_pkg

        store = SQLiteSkillStore(SKILL_DB_PATH)
        builtin_dir = Path(skill_pkg.__file__).parent / "builtin_skills"
        if builtin_dir.exists():
            store.discover([builtin_dir], origin="BUILTIN")
        _skill_store = store
    return _skill_store


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


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


async def _stream_chat(request: ChatRequest):
    thread_id = request.thread_id or f"gui_{uuid.uuid4().hex[:12]}"
    yield _sse({"type": "thread", "thread_id": thread_id})

    try:
        # 串行化：agent 内部状态非并发安全，单例 agent 一次只处理一轮对话
        async with _chat_lock:
            agent = get_agent()
            async for event in agent.astream(request.message, thread_id=thread_id):
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
    except Exception as exc:
        logger.exception("chat stream failed")
        yield _sse({"type": "error", "message": str(exc)})
    finally:
        yield _sse({"type": "done"})


app = FastAPI(title="NovaMind WebUI")


@app.post("/chat")
async def chat(request: ChatRequest):
    return StreamingResponse(
        _stream_chat(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/doctor")
async def doctor():
    """架构健康体检：复用 novamind doctor 的分层自检，返回结构化报告。"""
    try:
        from novamind.core.doctor import run_doctor

        return run_doctor().as_dict()
    except Exception as exc:
        logger.exception("doctor failed")
        return {
            "ok": False,
            "counts": {"error": 1, "warning": 0, "info": 0},
            "findings": [{
                "level": "error",
                "code": "doctor_failed",
                "message": str(exc),
                "suggestion": "Check the server log for the full traceback.",
            }],
        }


@app.get("/monitor/sessions")
async def monitor_sessions():
    """列出所有可监控的会话日志（logs/*.jsonl）。"""
    if not os.path.isdir(LOG_DIR):
        return {"sessions": []}
    sessions: list[dict] = []
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
        sessions.append({
            "thread_id": fname[:-6],
            "size_bytes": st.st_size,
            "last_modified": st.st_mtime,
        })
    sessions.sort(key=lambda s: s["last_modified"], reverse=True)
    return {"sessions": sessions}


@app.get("/monitor/events/{thread_id}")
async def monitor_events(thread_id: str):
    """返回指定会话的审计事件流（解析后的 JSON 列表）。"""
    path = os.path.join(LOG_DIR, f"{_safe_id(thread_id)}.jsonl")
    if not os.path.isfile(path):
        return {"events": []}
    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {"events": []}
    return {"events": events}


@app.get("/skills")
async def list_skills():
    """列出所有激活技能（名称/描述/四计数器/effective_rate）。"""
    try:
        store = get_skill_store()
        recs = store.list_active()
    except Exception as exc:
        logger.exception("list skills failed")
        return {"skills": [], "count": 0, "error": str(exc)}

    skills = []
    for r in recs:
        skills.append({
            "name": r.name,
            "description": r.description,
            "selections": r.total_selections,
            "applied": r.total_applied,
            "completions": r.total_completions,
            "fallbacks": r.total_fallbacks,
            "effective_rate": round(r.effective_rate, 3),
            "enabled": r.enabled,
            "is_active": r.is_active,
        })
    skills.sort(key=lambda s: s["name"])
    return {"skills": skills, "count": len(skills)}


@app.get("/sessions")
async def list_sessions():
    """列出所有会话（供前端侧边栏）。"""
    try:
        store = get_history_store()
        return {"sessions": store.list_threads()}
    except Exception:
        return {"sessions": []}


@app.get("/history/{thread_id}")
async def get_history(thread_id: str):
    """返回某会话的消息历史（供前端切换会话时加载）。"""
    try:
        store = get_history_store()
        msgs = store.load_messages(thread_id)
    except Exception:
        return {"messages": []}

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
    return {"messages": items}


@app.delete("/sessions/{thread_id}")
async def delete_session(thread_id: str):
    """删除指定会话（前端侧边栏删除按钮）。"""
    try:
        # 与流式对话共用锁：避免正在结束的 Agent 在删除后把旧状态重新落盘。
        async with _chat_lock:
            if _agent is not None:
                _agent.clear_conversation(thread_id)
            else:
                get_history_store().clear_thread(thread_id)
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# 静态文件（前端 index.html）最后挂载，避免拦截 /chat /health
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
