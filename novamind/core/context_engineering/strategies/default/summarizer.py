"""SummarizerExecutor — P4 全量 summarize + pairing 保护。

吸收 Poirot `context_engineering/strategies/default/summarizer.py` 的分区/配对保护逻辑，
融合 NovaMind 的摘要压缩（LLM 摘要 + 只记对话进度不记静态偏好）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from ...contract import GovernanceResult
from ._constants import CST

if TYPE_CHECKING:
    from .externalizer import ExternalizerExecutor

_SUMMARY_KEY = "_novamind_summary"


class SummarizerExecutor:
    """P4 全量 summarize + pairing 保护。"""

    def __init__(self, model: Any = None, preserve_recent: int = 6) -> None:
        self._model = model
        self._preserve_recent = preserve_recent

    def summarize_if_pending(
        self, governance: dict | None, messages: list, externalizer: "ExternalizerExecutor"
    ) -> GovernanceResult | None:
        governance = governance or {}
        pending = (governance.get("default") or {}).get("pending") or []
        if "P4" not in pending:
            return None
        if not self._model:
            return None
        to_summarize, preserved = self._partition(messages)
        if not to_summarize:
            return None
        summary_text = self._call_llm(to_summarize)
        if summary_text is None:
            summary_text = "压缩失败，保留最近对话。"
        summary_msg = HumanMessage(content=summary_text, additional_kwargs={_SUMMARY_KEY: True})
        remove_ids = [m.id for m in to_summarize if getattr(m, "id", None)]
        return GovernanceResult(
            state_patch={"governance": self._update_summary(governance, summary_text)},
            messages_patch=[RemoveMessage(id=i) for i in remove_ids] + [summary_msg, *preserved],
        )

    def _partition(self, messages: list) -> tuple[list, list]:
        n = len(messages)
        if n <= self._preserve_recent:
            return [], messages
        cut = n - self._preserve_recent
        cut = self._snap_to_pairing(messages, cut)
        to_summarize = list(messages[:cut])
        preserved = list(messages[cut:])
        preserved, orphans = self._strip_orphan_tools(preserved)
        to_summarize.extend(orphans)
        return to_summarize, preserved

    @staticmethod
    def _strip_orphan_tools(preserved: list) -> tuple[list, list]:
        ai_tc_ids: set[str] = set()
        for msg in preserved:
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if tc_id:
                        ai_tc_ids.add(tc_id)
        tool_ids: set[str] = set()
        for msg in preserved:
            if isinstance(msg, ToolMessage):
                tool_ids.add(msg.tool_call_id)
        clean: list = []
        orphans: list = []
        for msg in preserved:
            is_orphan_tool = isinstance(msg, ToolMessage) and msg.tool_call_id not in ai_tc_ids
            is_orphan_ai = isinstance(msg, AIMessage) and bool(msg.tool_calls) and not all(
                (tc.get("id") if isinstance(tc, dict) else None) in tool_ids
                for tc in msg.tool_calls
            )
            if is_orphan_tool or is_orphan_ai:
                orphans.append(msg)
            else:
                clean.append(msg)
        return clean, orphans

    @staticmethod
    def _snap_to_pairing(messages: list, cut: int) -> int:
        while cut < len(messages) and isinstance(messages[cut], ToolMessage):
            if cut > 0 and isinstance(messages[cut - 1], AIMessage) and messages[cut - 1].tool_calls:
                cut -= 1
            else:
                break
        return cut

    def _call_llm(self, messages: list) -> str | None:
        if not self._model:
            return None
        try:
            history = self._format_history(messages)
            prompt = (
                "你是一个负责维护 AI 工作台上下文的后台模块。\n\n"
                f"【刚刚过去的旧对话】\n{history}\n\n"
                "任务：请仔细阅读旧对话，提取出当前的对话语境和任务进度。\n"
                "要求：只记录'我们在聊什么'、'解决了什么问题'、'得出了什么结论'等。\n"
                "绝对不要记录用户的静态偏好(如姓名、职业、爱好等)，这部分由其他模块负责！\n"
                "客观、精简，直接返回最新的记忆文本，不要输出任何解释性废话。"
            )
            response = self._model.invoke(prompt, config={"tags": ["internal_llm"]})
            return response.content.strip() if hasattr(response, "content") else str(response)
        except Exception:
            return None

    @staticmethod
    def _format_history(messages: list) -> str:
        lines: list[str] = []
        for msg in messages:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[{type(msg).__name__}] {content[:500]}")
        return "\n".join(lines)

    def _update_summary(self, governance: dict, summary_text: str) -> dict:
        g = dict(governance or {})
        d = dict(g.get("default") or {})
        d["summary"] = summary_text
        d["summary_id"] = "summary_" + datetime.now(CST).strftime("%H%M%S%f")
        metrics = dict(d.get("metrics") or {})
        metrics["summarize_count"] = metrics.get("summarize_count", 0) + 1
        d["metrics"] = metrics
        g["default"] = d
        return g
