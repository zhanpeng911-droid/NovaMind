"""委派工具 — 动态生成 delegate_to_specialist / delegate_to_subagent tool。

吸收 Poirot `multiagent/tools.py`：LLM 只填 goal + success_criteria + sandbox_id 可选，
共享沙箱从 ContextVar 里的 ThreadState 透传（不创建新沙箱）。
"""

from __future__ import annotations

import contextvars
import json

from langchain_core.tools import tool

from .exceptions import SpecialistError, SubagentError
from .specialist import SpecialistAgent
from .subagent import SubagentProvider
from .types import SpecialistRequest, SubagentRequest

_current_state: contextvars.ContextVar[dict | None] = contextvars.ContextVar("multiagent_state", default=None)


def set_current_state(state: dict) -> None:
    """OrchestrationMiddleware 调用：设置当前 ThreadState 供 tool handler 读取。"""
    _current_state.set(state)


def get_current_state() -> dict:
    return _current_state.get() or {}


def _extract_sandbox_id(state: dict, sandbox_id: str | None) -> str | None:
    if sandbox_id is not None:
        return sandbox_id
    sandbox = state.get("sandbox")
    if isinstance(sandbox, dict):
        return sandbox.get("sandbox_id")
    return None


def _context_summary(state: dict, goal: str, success_criteria: str) -> str:
    """轻量上下文摘要：goal + success_criteria + 最近消息片段。"""
    messages = state.get("messages", []) or []
    recent = ""
    if messages:
        last = messages[-1]
        content = getattr(last, "content", "") if not isinstance(last, dict) else last.get("content", "")
        recent = str(content)[:500]
    return f"goal: {goal}\ncriteria: {success_criteria}\nrecent: {recent}"


def _result_summary(raw_output: str, artifacts: list, goal: str, success_criteria: str, specialist_name: str):
    """轻量结果摘要：raw output 截断 + 简单成功判定（黑盒，MVP 由 LLM 决策重试）。"""
    from .types import SpecialistResult

    return SpecialistResult(
        specialist_name=specialist_name,
        summary=raw_output[:2000],
        artifacts=tuple(artifacts),
        success=bool(raw_output.strip()),
        gap_analysis="" if raw_output.strip() else "specialist 未返回有效输出",
    )


def make_specialist_tool(name: str, specialist: SpecialistAgent, *, max_steps: int = 50, timeout_seconds: int = 600):
    """为 specialist 动态生成 delegate_to_<name> tool。"""

    @tool(f"delegate_to_{name}")
    def delegate_tool(goal: str, success_criteria: str, sandbox_id: str | None = None) -> str:
        """Delegate task to specialist. Provide goal and success_criteria. sandbox_id optional (uses thread sandbox if omitted)."""
        state = get_current_state()
        resolved_sandbox_id = _extract_sandbox_id(state, sandbox_id)
        context_summary = _context_summary(state, goal, success_criteria)

        request = SpecialistRequest(
            goal=goal,
            success_criteria=success_criteria,
            context_summary=context_summary,
            sandbox_id=resolved_sandbox_id,
            artifacts_path=(state.get("metadata") or {}).get("artifacts_path"),
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )
        try:
            raw = specialist.invoke(request)
        except SpecialistError as e:
            return json.dumps({
                "success": False,
                "error": {"type": type(e).__name__, "message": str(e)},
                "suggestion": "retry, fallback to another specialist, or self-do",
            }, ensure_ascii=False)

        result = _result_summary(raw.raw_output, list(raw.artifacts), goal, success_criteria, specialist.name)
        return json.dumps({
            "success": result.success,
            "summary": result.summary,
            "specialist": result.specialist_name,
            "gap_analysis": result.gap_analysis,
            "artifacts": [{"path": a.path, "type": a.artifact_type} for a in result.artifacts],
        }, ensure_ascii=False)

    return delegate_tool


def make_subagent_tool(subagent_provider: SubagentProvider, *, max_steps: int = 20, timeout_seconds: int = 300):
    """生成 delegate_to_subagent tool（self-copy subagent，leaf role，shared sandbox）。"""

    @tool("delegate_to_subagent")
    def delegate_tool(goal: str, success_criteria: str, sandbox_id: str | None = None) -> str:
        """Delegate task to a self-copy subagent (leaf role, isolated context, shared sandbox)."""
        state = get_current_state()
        resolved_sandbox_id = _extract_sandbox_id(state, sandbox_id)
        context_summary = _context_summary(state, goal, success_criteria)

        request = SubagentRequest(
            goal=goal,
            success_criteria=success_criteria,
            context_summary=context_summary,
            sandbox_id=resolved_sandbox_id,
            artifacts_path=(state.get("metadata") or {}).get("artifacts_path"),
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )
        try:
            sub_result = subagent_provider.spawn(request)
        except SubagentError as e:
            return json.dumps({
                "success": False,
                "error": {"type": type(e).__name__, "message": str(e)},
                "suggestion": "retry, fallback to specialist, or self-do",
            }, ensure_ascii=False)

        return json.dumps({
            "success": sub_result.success,
            "summary": sub_result.summary,
            "specialist": "subagent",
            "gap_analysis": sub_result.gap_analysis,
            "artifacts": [{"path": a.path, "type": a.artifact_type} for a in sub_result.artifacts],
        }, ensure_ascii=False)

    return delegate_tool
