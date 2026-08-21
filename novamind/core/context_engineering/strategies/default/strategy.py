"""DefaultStrategy — 上下文治理默认策略（P0-P5 分段优先级舍弃）。

吸收 Poirot `context_engineering/strategies/default/strategy.py`：
- P1 externalize 0.40（历史消息外化到文件）
- P2 thinking 0.50
- P3 observations 0.60（top-N 截断）
- P4 summarize 0.80（compaction 摘要）
- P5 stop_toolcall 0.90（剥 tool_calls + 收尾提示 + jump model）
- hard_stop 0.99（强制收尾）

融合 NovaMind：摘要 prompt 只记对话进度不记静态偏好；token 估算 CJK-aware。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ...contract import GovernanceContext, GovernanceResult
from ...utilities import resolve_window_size
from ._constants import DEFAULT_THRESHOLDS
from .budget import BudgetTrackerExecutor
from .externalizer import ExternalizerExecutor
from .snapshot import SnapshotExecutor
from .summarizer import SummarizerExecutor

logger = logging.getLogger(__name__)


class DefaultStrategy:
    """默认上下文治理策略：分段优先级舍弃 P0-P5。"""

    def __init__(self, params: dict | None = None, model: Any = None, summarize_model: Any = None) -> None:
        params = params or {}
        self._model = model
        self._thresholds: dict[str, float] = {**DEFAULT_THRESHOLDS, **(params.get("thresholds") or {})}
        self._budget = BudgetTrackerExecutor(self._thresholds)
        self._externalizer = ExternalizerExecutor(
            externalize_dir=params.get("externalize_dir", ".novamind/externalized"),
            min_chars=params.get("externalize_min_chars", 500),
            preview_chars=params.get("externalize_preview_chars", 500),
            exempt_rounds=params.get("exempt_rounds", 2),
            tool_metadata=params.get("tool_metadata"),
        )
        self._summarizer = SummarizerExecutor(
            model=summarize_model or model,
            preserve_recent=params.get("preserve_recent", 6),
        )
        self._snapshot = SnapshotExecutor(snapshot_dir=params.get("snapshot_dir", ".novamind/snapshots"))

    def before_agent(self, ctx: GovernanceContext) -> GovernanceResult:
        governance = self._budget.init_budget(ctx.governance)
        return GovernanceResult(state_patch={"governance": governance})

    def after_agent(self, ctx: GovernanceContext) -> GovernanceResult:
        governance = self._budget.clear_run_state(ctx.governance)
        return GovernanceResult(state_patch={"governance": governance})

    def before_model(self, ctx: GovernanceContext) -> GovernanceResult:
        pairing_patch = self._ensure_pairing(ctx.messages or [])

        governance = ctx.governance or {}
        d = governance.get("default") or {}
        pending = d.get("pending") or []
        fraction = (d.get("budget") or {}).get("fraction", 0.0)

        if "P4" in pending:
            logger.info("compaction P4 summarize 触发 fraction=%.2f", fraction)
            snapshot_gov = self._snapshot.snapshot_if_pending(ctx.governance, ctx.messages or [], ctx.state)
            effective_gov = snapshot_gov or ctx.governance
            result = self._summarizer.summarize_if_pending(effective_gov, ctx.messages or [], self._externalizer)
            if result is not None:
                if pairing_patch:
                    combined = (result.messages_patch or []) + pairing_patch
                    return GovernanceResult(state_patch=result.state_patch, messages_patch=combined, jump_to=result.jump_to)
                return result

        if "P1" in pending:
            skip_until = d.get("p1_skip_until_fraction", 0.0)
            if fraction < skip_until:
                logger.info("compaction P1 skipped (interval suppression)")
            else:
                logger.info("compaction P1 externalize 触发 fraction=%.2f", fraction)
                messages_patch = self._externalizer.externalize_history(ctx.messages or [])
                ext_count = len(messages_patch) if messages_patch else 0
                d["p1_skip_until_fraction"] = fraction + 0.10
                d["p1_completed"] = (ext_count == 0)
                governance["default"] = d
                if messages_patch:
                    if pairing_patch:
                        messages_patch = messages_patch + pairing_patch
                    return GovernanceResult(state_patch={"governance": governance}, messages_patch=messages_patch)
                if pairing_patch:
                    return GovernanceResult(state_patch={"governance": governance}, messages_patch=pairing_patch)
                return GovernanceResult(state_patch={"governance": governance})

        if pairing_patch:
            return GovernanceResult(messages_patch=pairing_patch)
        return GovernanceResult()

    def after_model(self, ctx: GovernanceContext) -> GovernanceResult:
        messages = ctx.messages or []
        config = ctx.config or {}
        config_window = config.get("window") if isinstance(config, dict) else None
        window = config_window or resolve_window_size(self._model)
        governance = self._budget.track(ctx.governance, messages, ctx.token_counter, window)

        d = governance.get("default") or {}
        pending = d.get("pending") or []
        warned = d.get("warned", False)
        fraction = (d.get("budget") or {}).get("fraction", 0.0)

        if "P5" in pending and not warned:
            last_ai = next(
                (m for m in reversed(messages) if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)),
                None,
            )
            if last_ai:
                cleared = self._strip_tool_calls(last_ai, fraction)
                stop_msg = HumanMessage(
                    name="context_budget_stop",
                    additional_kwargs={"hide_from_ui": True},
                    content=self._build_stop_message(fraction),
                )
                d["warned"] = True
                governance["default"] = d
                return GovernanceResult(
                    state_patch={"governance": governance},
                    messages_patch=[cleared, stop_msg],
                    jump_to="model",
                )

        return GovernanceResult(state_patch={"governance": governance})

    def wrap_tool_call(self, ctx: GovernanceContext) -> GovernanceResult:
        tool_result = ctx.tool_result
        if isinstance(tool_result, ToolMessage):
            rewritten = self._externalizer.externalize_if_needed(tool_result)
            if rewritten is not None:
                return GovernanceResult(override=rewritten)
        return GovernanceResult()

    @staticmethod
    def _ensure_pairing(messages: list) -> list | None:
        tool_msg_ids = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        patch = []
        for msg in messages:
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if tc_id and tc_id not in tool_msg_ids:
                        patch.append(ToolMessage(content="⚠️ 工具结果缺失，已补占位。", tool_call_id=tc_id, status="error"))
        return patch or None

    @staticmethod
    def _strip_tool_calls(last_ai: AIMessage, fraction: float) -> AIMessage:
        new_kwargs = {
            k: v for k, v in (last_ai.additional_kwargs or {}).items()
            if k not in ("tool_calls", "function_call")
        }
        new_kwargs["context_budget_stop"] = round(fraction, 4)
        return AIMessage(id=last_ai.id, content=last_ai.content or "", tool_calls=[], additional_kwargs=new_kwargs)

    @staticmethod
    def _build_stop_message(fraction: float) -> str:
        if fraction >= 0.99:
            return (
                "<system_reminder>\n"
                "上下文窗口占用已达 99% 硬底线，强制收尾。"
                "不可再调用任何工具，请立即基于已有信息给出最终答案。\n"
                "</system_reminder>"
            )
        return (
            "<system_reminder>\n"
            f"上下文窗口占用已达 {fraction:.0%}，超过 90% 阈值。"
            "请基于已有信息收尾，不再调用工具。若证据不足，基于现有信息给出最佳答案并说明缺口。\n"
            "</system_reminder>"
        )
