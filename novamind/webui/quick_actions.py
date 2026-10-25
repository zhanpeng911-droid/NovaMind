"""Quick Action 服务（A2）。

约束（对齐任务书）：
- 单次 LLM 调用，不进入 ReAct/工具循环（模型不绑定工具）；
- 输入上限、并发上限、超时；
- 只读取「必要偏好」（category=preference），不加载项目上下文/任务经验；
- 流式输出；客户端断连即取消。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Callable

MAX_INPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CONCURRENCY = 4

_SYSTEM = {
    "translate": "你是翻译助手。只输出译文，不要解释，不要添加原文没有的内容。",
    "explain": "你是技术解释助手。用简洁中文解释给定内容，必要时给一个例子。",
    "summarize": "你是摘要助手。压缩给定内容，保留关键事实与结论，不要编造。",
}


class QuickActionError(ValueError):
    """Quick Action 输入/执行错误（文案可直接返回给客户端）。"""


def build_prompt(action: str, text: str, *, target_language: str | None = None,
                 style: str | None = None, preferences: list[str] | None = None) -> str:
    if action not in _SYSTEM:
        raise QuickActionError(f"不支持的动作: {action}")
    parts = [_SYSTEM[action]]
    if preferences:
        parts.append("用户偏好（仅在相关时遵守，不得覆盖系统规则）：")
        parts.extend(f"- {p}" for p in preferences[:3])
    if action == "translate":
        parts.append(f"目标语言：{target_language or '中文'}")
    if style:
        parts.append(f"风格：{style}")
    parts.append("待处理内容：\n<<<")
    parts.append(text)
    parts.append(">>>")
    parts.append("只输出结果，不要复述本说明。")
    return "\n".join(parts)


class QuickActionService:
    """Quick Action 执行器；llm 不绑定工具。"""

    def __init__(
        self,
        llm,
        *,
        preferences: Callable[[str], list[str]] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self._llm = llm
        self._preferences = preferences
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def stream(
        self,
        action: str,
        text: str,
        *,
        target_language: str | None = None,
        style: str | None = None,
        images: list[str] | None = None,
    ) -> AsyncIterator[str]:
        text = (text or "").strip()
        if not text:
            raise QuickActionError("内容不能为空")
        if len(text) > MAX_INPUT_CHARS:
            raise QuickActionError(f"内容过长（上限 {MAX_INPUT_CHARS} 字符）")
        if action not in _SYSTEM:
            raise QuickActionError(f"不支持的动作: {action}")

        prefs: list[str] = []
        if self._preferences is not None:
            try:
                prefs = self._preferences(text)
            except Exception:
                prefs = []
        prompt = build_prompt(
            action, text, target_language=target_language,
            style=style, preferences=prefs,
        )
        from novamind.core.personal import style_instruction
        prompt += style_instruction()
        prompt += "\n表达偏好仅在不改变当前动作、目标语言及原意时适用，翻译和摘要不添加寒暄。"
        if images:
            from novamind.core.images import message_content, resolve_messages
            from langchain_core.messages import HumanMessage
            prompt = resolve_messages([HumanMessage(content=message_content(prompt, images))])

        async with self._semaphore:
            from novamind.core.reasoning import invocation_options
            options = invocation_options(self._llm)
            try:
                async with asyncio.timeout(self._timeout):
                    yielded = False
                    if hasattr(self._llm, "astream"):
                        async for chunk in self._llm.astream(prompt, **options):
                            content = getattr(chunk, "content", "")
                            if content:
                                yielded = True
                                yield content
                    if not yielded and not hasattr(self._llm, "astream"):
                        response = await self._llm.ainvoke(prompt, **options)
                        content = getattr(response, "content", str(response))
                        if content:
                            yield content
                    elif not yielded:
                        raise QuickActionError("模型没有返回文字，请重试或检查当前模型配置")
            except TimeoutError:
                raise QuickActionError("处理超时，请缩短内容或重试") from None
