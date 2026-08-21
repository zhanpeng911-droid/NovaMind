"""ExternalizerExecutor — 外化超长 tool result 到磁盘 + preview。

吸收 Poirot `context_engineering/strategies/default/externalizer.py`：
- P1 externalize_history：FIFO 时序 + 近 N 轮豁免 + 每轮保 1 + 幂等 + 同步写盘
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from ._constants import CST

logger = logging.getLogger(__name__)

_EXTERNALIZED_KEY = "_novamind_externalized"
_EXTERNALIZED_PATH_KEY = "_novamind_externalized_path"
_EXTERNALIZED_META_KEY = "_novamind_externalized_meta"


class ExternalizerExecutor:
    def __init__(
        self,
        externalize_dir: str = ".novamind/externalized",
        min_chars: int = 500,
        preview_chars: int = 500,
        exempt_rounds: int = 2,
        tool_metadata: dict[str, dict] | None = None,
    ) -> None:
        self._dir = externalize_dir
        self._min_chars = min_chars
        self._preview_chars = preview_chars
        self._exempt_rounds = exempt_rounds
        self._tool_metadata = tool_metadata or {}

    def _get_threshold(self, tool_name: str | None) -> int:
        if tool_name and tool_name in self._tool_metadata:
            typical_tokens = self._tool_metadata[tool_name].get("typical_output_tokens", 0)
            if typical_tokens > 0:
                return max(self._min_chars, typical_tokens * 4)
        return self._min_chars

    def externalize_if_needed(self, tool_result: ToolMessage) -> ToolMessage | None:
        if tool_result.additional_kwargs.get(_EXTERNALIZED_KEY):
            return None
        text = self._extract_text(tool_result.content)
        threshold = self._get_threshold(tool_result.name)
        if len(text) <= threshold:
            return None
        path = self._write_to_disk(text, tool_result.tool_call_id, tool_result.name)
        if path is None:
            return None
        preview = text[: self._preview_chars] + f"\n\n[externalized path={path} tokens~{len(text) // 4}]"
        meta = {"tool_name": tool_result.name or "", "tokens_saved": len(text) // 4, "created_at": datetime.now(CST).isoformat()}
        new_kwargs = {**tool_result.additional_kwargs, _EXTERNALIZED_KEY: True, _EXTERNALIZED_PATH_KEY: path, _EXTERNALIZED_META_KEY: meta}
        return tool_result.model_copy(update={"content": preview, "additional_kwargs": new_kwargs})

    def externalize_history(self, messages: list) -> list[ToolMessage] | None:
        turns = self._partition_turns(messages)
        if len(turns) <= self._exempt_rounds:
            return None

        exempt_start = len(turns) - self._exempt_rounds
        candidates: list[tuple[int, ToolMessage]] = []

        for turn_idx in range(exempt_start):
            tool_msgs = [(mi, m) for mi, m in turns[turn_idx] if isinstance(m, ToolMessage)]
            if len(tool_msgs) <= 1:
                continue
            for mi, m in tool_msgs[:-1]:
                if m.additional_kwargs.get(_EXTERNALIZED_KEY):
                    continue
                text = self._extract_text(m.content)
                if len(text) <= self._min_chars:
                    continue
                candidates.append((mi, m))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])

        rewritten_list: list[ToolMessage] = []
        for _, msg in candidates:
            rewritten = self.externalize_if_needed(msg)
            if rewritten is not None:
                rewritten_list.append(rewritten)

        return rewritten_list if rewritten_list else None

    @staticmethod
    def _partition_turns(messages: list) -> list[list[tuple[int, Any]]]:
        turns: list[list[tuple[int, Any]]] = []
        current: list[tuple[int, Any]] = []
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                if current:
                    turns.append(current)
                current = [(i, msg)]
            else:
                current.append((i, msg))
        if current:
            turns.append(current)
        return turns

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    parts.append(str(item["text"]))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        return str(content) if content is not None else ""

    def _write_to_disk(self, content: str, tool_call_id: str | None, tool_name: str | None) -> str | None:
        try:
            os.makedirs(self._dir, exist_ok=True)
            safe_name = (tool_name or "unknown").replace("/", "_").replace("\\", "_")
            short_id = (tool_call_id or uuid.uuid4().hex)[:12]

            try:
                parsed = json.loads(content)
                ext = ".json"
                write_content = json.dumps(parsed, indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                ext = ".txt"
                write_content = content

            filename = f"{safe_name}-{short_id}{ext}"
            filepath = os.path.join(self._dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(write_content)
            return filepath
        except (OSError, TypeError) as exc:
            logger.error("externalize write failed: %s", exc)
            return None
