"""NovaMind 测试共享基础设施。

集中定义可编程 FakeLLM 与审计收集器，消除各测试文件的重复定义。
设计对齐 Poirot 的 tests/v1/_fake_model.py：一个共享 fake 解决集成测试里
bind_tools / 预设响应序列 / 调用历史记录的通用需求。

用法（向后兼容各文件原有的本地定义）：
    from _fakes import FakeLLM, FakeAuditLogger, FakeAudit

    fake = FakeLLM(responses=[
        AIMessage(content="hi"),
        AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}]),
    ])
    fake.call_count   # 已调用次数
    fake.call_history  # 每次 invoke 的 messages
    fake.bound_tools  # bind_tools 传入的工具列表

    audit = FakeAuditLogger()  # events 为 dict 列表，支持 get_events 过滤
    audit2 = FakeAudit()       # events 为 (event, kwargs) 元组列表（wiring 测试用）
"""
from __future__ import annotations

import os

from langchain_core.messages import AIMessage


def env_without(*prefixes: str) -> dict[str, str]:
    """返回剔除指定前缀环境变量后的完整副本，供 patch.dict(os.environ, ..., clear=True) 用。

    不要直接 clear=True 清空整个 environ：Windows 上 uv 独立版 CPython 的
    OpenSSL 初始化依赖 SystemRoot 等系统变量，全清会让 langchain-openai
    首次导入时抛 ssl.SSLError（表现为"缺 key"测试误报环境崩溃）。
    """
    return {
        k: v for k, v in os.environ.items()
        if not any(k.startswith(p) for p in prefixes)
    }


class FakeLLM:
    """可编程假 LLM：按预设序列返回响应，超出后回退默认回复。"""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self._call_count = 0
        self.call_history = []
        self.bound_tools = None

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = tools
        return self

    def invoke(self, messages, **kwargs):
        self.call_history.append(messages)
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return AIMessage(content="[FakeLLM default]")

    @property
    def call_count(self) -> int:
        return self._call_count


class FakeAuditLogger:
    """收集审计事件（dict 形式），支持按事件类型过滤。"""

    def __init__(self):
        self.events = []

    def log_event(self, thread_id: str, event: str, **kwargs):
        self.events.append({"thread_id": thread_id, "event": event, **kwargs})

    def get_events(self, event_type: str):
        return [e for e in self.events if e["event"] == event_type]


class FakeAudit:
    """收集审计事件（(event, kwargs) 元组形式），wiring 测试用。"""

    def __init__(self):
        self.events = []

    def log_event(self, thread_id, event, **kwargs):
        self.events.append((event, kwargs))
