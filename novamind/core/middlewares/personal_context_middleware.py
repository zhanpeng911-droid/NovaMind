"""Provide the same explicit user-intent context for CLI and Web entry points."""
from contextvars import ContextVar
from .protocol import BaseAgentMiddleware
from ..personal import request_context

_token = ContextVar("cli_personal_context_token", default=None)


class PersonalContextMiddleware(BaseAgentMiddleware):
    async def abefore_agent(self, ctx):
        if request_context.get().get("turn_id"):
            return None
        messages = getattr(ctx.state, "messages", [])
        last = next((m for m in reversed(messages) if getattr(m, "type", "") == "human"), None)
        text = last.content if last and isinstance(last.content, str) else ""
        token = request_context.set({"thread_id": ctx.thread_id, "turn_id": getattr(last, "id", ""),
                                     "text": text, "scope": "global", "memory_mode": "normal"})
        _token.set(token)

    async def aafter_agent(self, ctx):
        token = _token.get()
        if token is not None:
            request_context.reset(token)
            _token.set(None)
