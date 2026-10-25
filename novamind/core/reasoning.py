"""Per-call reasoning options; never mutate a shared model between sessions."""
from __future__ import annotations

import re
from typing import Any

LEVELS = ("auto", "quick", "low", "high", "max")


def is_greeting(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(你好|您好|嗨|哈喽|hello|hi|hey|早上好|晚上好)[!！。.~～\s]*", text, re.I))


def invocation_options(model: Any, level: str | None = None) -> dict:
    from .personal import request_context
    context = request_context.get()
    level = level or context.get("reasoning", "auto")
    if level == "auto":
        level = "quick" if is_greeting(context.get("text", "")) else "low"
    while hasattr(model, "bound"):
        model = model.bound
    if getattr(model, "_llm_type", "") == "fallback-chat-model":
        return {"novamind_effort": level}
    name = str(getattr(model, "model_name", "") or getattr(model, "model", "")).lower()
    base = str(getattr(model, "openai_api_base", "") or "").lower()
    if "deepseek" in name or "api.deepseek.com" in base:
        options = {"extra_body": {"thinking": {"type": "disabled" if level == "quick" else "enabled"}}}
        if level != "quick":
            options["reasoning_effort"] = level
        if is_greeting(context.get("text", "")):
            options["tool_choice"] = "none"
        return options
    if name.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6")) and (not base or "api.openai.com" in base):
        # low/high are the common subset; do not send model-specific max/none.
        return {"reasoning_effort": "low" if level in {"quick", "low"} else "high"}
    return {}
