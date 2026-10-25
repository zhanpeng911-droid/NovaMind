"""Personal skills are selected by reference and injected only for this call."""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

from langchain_core.messages import HumanMessage

from ...middlewares.protocol import BaseAgentMiddleware, MiddlewareResult
from .policy import LearningError, digest

selected: ContextVar[dict | None] = ContextVar("personal_skill_selection", default=None)


def visible(store, skill_id, context, *, return_content=False):
    with store.tx() as conn:
        row = conn.execute("SELECT 1 FROM learning_skills WHERE id=?", (skill_id,)).fetchone()
        if not row:
            return True  # Ordinary imported skills retain their existing contract.
        skill = store.get(conn, "skills", skill_id)
        record = conn.execute("SELECT enabled,is_active,path,content_hash FROM skill_records WHERE skill_id=?", (skill_id,)).fetchone()
        if (not record or not record["enabled"] or not record["is_active"] or skill["state"] != "active"
                or context.get("memory_mode") == "private" or skill["scope"] not in {"global", context.get("scope", "global")}):
            return False
        try:
            candidate = store.get(conn, "candidates", skill["candidate_id"])
            store.check_candidate(conn, candidate)
            path = Path(record["path"])
            root = (store.root / "learned_skills").resolve()
            if not root.is_relative_to(store.root.resolve()) or not path.resolve().is_relative_to(root):
                return False
            with path.open(encoding="utf-8") as source:
                content = source.read(20001)
            if len(content) > 20000 or digest(content)[:16] != record["content_hash"]:
                return False
            return content if return_content else True
        except (LearningError, OSError):
            return False


class PersonalSkillMiddleware(BaseAgentMiddleware):
    def __init__(self, store):
        self.store = store

    async def abefore_agent(self, ctx):
        # The object, unlike ContextVar.set in a tool thread, survives to_thread.
        selected.set({"ids": []})

    async def abefore_model(self, ctx):
        from ...personal import request_context
        messages = []
        for skill_id in (selected.get() or {}).get("ids", []):
            content = visible(self.store, skill_id, dict(request_context.get()), return_content=True)
            if isinstance(content, str):
                messages.append(HumanMessage(content="[已批准的个人方法，不授予额外权限]\n" + content,
                    name="personal_skill", additional_kwargs={"hide_from_ui": True}))
                if ctx.state is not None:
                    used = ctx.state.metadata.setdefault("learning_used", [])
                    if skill_id not in used:
                        used.append(skill_id)
        if messages:
            return MiddlewareResult(messages_patch=messages)

    async def aafter_agent(self, ctx):
        selected.set(None)
