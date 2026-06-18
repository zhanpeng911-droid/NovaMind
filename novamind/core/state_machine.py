"""
NovaMind 自定义状态机引擎

替代 LangGraph 的 StateGraph，提供更轻量、更可控的智能体循环。
核心设计：
  - 基于 asyncio 的异步状态机
  - 节点（Node）+ 边（Edge）的有向图结构
  - 支持条件路由和工具调用循环
  - 内置审计日志和Token追踪
  - SQLite持久化（对话历史跨会话保存）
"""
from __future__ import annotations
import asyncio
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional
from langchain_core.messages import (
    BaseMessage, HumanMessage, SystemMessage,
    AIMessage, ToolMessage, RemoveMessage
)
from .logger import AuditLogger
from .token_tracker import TokenTracker
from .context import ContextManager


@dataclass
class AgentState:
    """
    智能体状态容器

    messages: 完整对话历史（由 add_messages 归约器管理）
    summary: 上下文压缩后的摘要文本
    metadata: 额外元数据（Token用量、轮次计数等）
    """
    messages: list[BaseMessage] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_message(self, msg: BaseMessage) -> None:
        """添加一条消息到历史记录"""
        self.messages.append(msg)

    def add_messages(self, msgs: list[BaseMessage]) -> None:
        """批量添加消息"""
        self.messages.extend(msgs)

    def remove_messages(self, msg_ids: list[str]) -> None:
        """按ID移除消息（用于上下文裁剪）"""
        id_set = set(msg_ids)
        self.messages = [m for m in self.messages if getattr(m, "id", None) not in id_set]


class ConversationStore:
    """
    对话持久化存储

    将对话历史序列化为JSON，存储到SQLite数据库。
    支持：
      - 跨会话持久化（重启后恢复对话）
      - 按thread_id隔离不同会话
      - 增量保存（每次新消息后追加）
      - 线程安全写入
    """

    def __init__(self, db_path: str):
        """
        Args:
            db_path: SQLite数据库文件路径
        """
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        import sqlite3
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    message_id TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    thread_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_message(self, thread_id: str, msg: BaseMessage) -> None:
        """保存单条消息到数据库"""
        with self._lock:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            try:
                # 序列化消息
                role = msg.type if hasattr(msg, "type") else "unknown"
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                message_id = getattr(msg, "id", None)
                name = getattr(msg, "name", None)
                tool_call_id = getattr(msg, "tool_call_id", None)

                # 序列化tool_calls
                tool_calls = None
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    tool_calls = json.dumps(msg.tool_calls, ensure_ascii=False)

                conn.execute(
                    """INSERT INTO conversations
                       (thread_id, role, content, message_type, message_id,
                        tool_calls, tool_call_id, name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (thread_id, role, content, type(msg).__name__,
                     message_id, tool_calls, tool_call_id, name)
                )
                conn.commit()
            finally:
                conn.close()

    def save_messages(self, thread_id: str, messages: list[BaseMessage]) -> None:
        """批量保存消息"""
        for msg in messages:
            self.save_message(thread_id, msg)

    def save_summary(self, thread_id: str, summary: str) -> None:
        """保存/更新对话摘要"""
        with self._lock:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO summaries (thread_id, summary, updated_at)
                       VALUES (?, ?, CURRENT_TIMESTAMP)""",
                    (thread_id, summary)
                )
                conn.commit()
            finally:
                conn.close()

    def load_messages(self, thread_id: str) -> list[BaseMessage]:
        """从数据库加载指定会话的完整消息历史"""
        import sqlite3
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute(
                    """SELECT role, content, message_type, message_id,
                              tool_calls, tool_call_id, name
                       FROM conversations
                       WHERE thread_id = ?
                       ORDER BY id ASC""",
                    (thread_id,)
                )
                messages = []
                for row in cursor:
                    role, content, msg_type, msg_id, tool_calls_json, tool_call_id, name = row

                    msg = None
                    if msg_type == "HumanMessage":
                        msg = HumanMessage(content=content)
                    elif msg_type == "AIMessage":
                        tc = json.loads(tool_calls_json) if tool_calls_json else []
                        msg = AIMessage(content=content, tool_calls=tc)
                    elif msg_type == "ToolMessage":
                        msg = ToolMessage(
                            content=content,
                            tool_call_id=tool_call_id or "",
                            name=name or "",
                        )
                    elif msg_type == "SystemMessage":
                        msg = SystemMessage(content=content)

                    if msg and msg_id:
                        msg.id = msg_id
                    if msg:
                        messages.append(msg)

                return messages
            finally:
                conn.close()

    def load_summary(self, thread_id: str) -> str:
        """加载指定会话的摘要"""
        import sqlite3
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute(
                    "SELECT summary FROM summaries WHERE thread_id = ?",
                    (thread_id,)
                )
                row = cursor.fetchone()
                return row[0] if row else ""
            finally:
                conn.close()

    def clear_thread(self, thread_id: str) -> None:
        """清除指定会话的所有数据"""
        with self._lock:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("DELETE FROM conversations WHERE thread_id = ?", (thread_id,))
                conn.execute("DELETE FROM summaries WHERE thread_id = ?", (thread_id,))
                conn.commit()
            finally:
                conn.close()


# 节点函数类型：接收状态，返回状态更新字典
NodeFunc = Callable[[AgentState], Awaitable[dict[str, Any]]]


class Node:
    """
    状态机节点

    每个节点封装一个处理函数，可以是：
    - agent_node: 调用LLM进行推理
    - tool_node: 执行工具调用
    - 自定义业务节点
    """
    def __init__(self, name: str, func: NodeFunc, description: str = ""):
        self.name = name
        self.func = func
        self.description = description

    async def execute(self, state: AgentState) -> dict[str, Any]:
        return await self.func(state)


class Edge:
    """
    状态机边：定义节点间的跳转关系

    支持两种模式：
    - 固定跳转：source -> target
    - 条件跳转：source -> condition_func -> {True: target_a, False: target_b}
    """
    def __init__(
        self,
        source: str,
        target: str | None = None,
        condition: Callable[[AgentState], str] | None = None,
        condition_map: dict[str, str] | None = None,
    ):
        self.source = source
        self.target = target
        self.condition = condition
        self.condition_map = condition_map

    def resolve(self, state: AgentState) -> str:
        """根据条件决定下一个节点"""
        if self.condition and self.condition_map:
            result = self.condition(state)
            return self.condition_map.get(result, "__end__")
        return self.target or "__end__"


class NovaMindAgent:
    """
    NovaMind 核心智能体引擎

    关键设计：
    - 状态在Agent级别维护（跨多次run/astream调用保持对话连贯）
    - 支持SQLite持久化（重启后恢复对话）
    - 节点+边的有向图结构

    用法：
        agent = NovaMindAgent()
        agent.add_node("agent", agent_func)
        agent.add_node("tools", tool_func)
        agent.add_edge("START", "agent")
        agent.add_conditional_edge("agent", route_fn, {"tools": "tools", "__end__": "__end__"})
        agent.add_edge("tools", "agent")

        result = await agent.run("你好")
        result = await agent.run("继续刚才的话题")  # 状态自动保持
    """

    def __init__(
        self,
        audit_logger: AuditLogger | None = None,
        token_tracker: TokenTracker | None = None,
        context_manager: ContextManager | None = None,
        conversation_store: ConversationStore | None = None,
    ):
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._logger = audit_logger
        self._token_tracker = token_tracker
        self._context_manager = context_manager
        self._store = conversation_store

        # 状态在Agent级别维护：跨run/astream调用保持对话连贯
        self._states: dict[str, AgentState] = {}
        # 持久化计数器：记录每个线程已保存的消息数量，避免全量加载
        self._persisted_counts: dict[str, int] = {}

    def _get_or_create_state(self, thread_id: str) -> AgentState:
        """
        获取或创建指定线程的状态

        如果状态已存在（同一会话内多次调用），直接复用。
        如果不存在，从数据库加载或创建新状态。
        """
        if thread_id in self._states:
            return self._states[thread_id]

        state = AgentState()
        state.metadata["thread_id"] = thread_id
        state.metadata["iteration"] = 0

        # 从数据库恢复对话历史（跨会话持久化）
        if self._store:
            saved_messages = self._store.load_messages(thread_id)
            if saved_messages:
                state.add_messages(saved_messages)
                self._persisted_counts[thread_id] = len(saved_messages)
                state.summary = self._store.load_summary(thread_id)

        self._states[thread_id] = state
        return state

    def _persist_state(self, thread_id: str, state: AgentState) -> None:
        """将本轮新增消息持久化到数据库（O(1)追加，不全量加载）"""
        if not self._store:
            return

        pending_key = "_pending_persist_messages"
        new_msgs = state.metadata.get(pending_key)
        if new_msgs is None:
            # 兼容旧状态：没有显式 pending 队列时退回旧计数逻辑。
            last_count = self._persisted_counts.get(thread_id, 0)
            new_msgs = state.messages[last_count:]

        if new_msgs:
            self._store.save_messages(thread_id, new_msgs)
            self._persisted_counts[thread_id] = self._persisted_counts.get(thread_id, 0) + len(new_msgs)
            state.metadata[pending_key] = []

        # 保存摘要（如果有更新）
        if state.summary:
            self._store.save_summary(thread_id, state.summary)

    def _mark_pending_persist(self, state: AgentState, messages: list[BaseMessage]) -> None:
        """记录本轮需要增量持久化的新消息。"""
        if not self._store or not messages:
            return
        state.metadata.setdefault("_pending_persist_messages", []).extend(messages)

    def add_node(self, name: str, func: NodeFunc, description: str = "") -> None:
        """注册一个处理节点"""
        self._nodes[name] = Node(name, func, description)

    def add_edge(self, source: str, target: str) -> None:
        """添加固定跳转边"""
        self._edges.append(Edge(source=source, target=target))

    def add_conditional_edge(
        self,
        source: str,
        condition: Callable[[AgentState], str],
        condition_map: dict[str, str],
    ) -> None:
        """添加条件跳转边"""
        self._edges.append(Edge(
            source=source,
            condition=condition,
            condition_map=condition_map,
        ))

    def _find_edge(self, source: str) -> Edge | None:
        """查找从指定节点出发的边"""
        for edge in self._edges:
            if edge.source == source:
                return edge
        return None

    async def run(
        self,
        user_input: str,
        thread_id: str = "default",
        max_iterations: int = 50,
    ) -> AgentState:
        """
        执行一次完整的智能体循环

        关键：状态在Agent级别维护，跨多次run调用保持对话连贯。

        Args:
            user_input: 用户输入文本
            thread_id: 会话线程ID（用于日志和状态追踪）
            max_iterations: 最大循环次数（防止无限循环）

        Returns:
            最终的智能体状态
        """
        state = self._get_or_create_state(thread_id)
        user_msg = HumanMessage(content=user_input, id=f"msg_{uuid.uuid4().hex[:12]}")
        state.add_message(user_msg)
        self._mark_pending_persist(state, [user_msg])
        state.metadata["iteration"] = 0

        current = "agent"
        visited_edges: list[str] = []

        while current != "__end__" and state.metadata["iteration"] < max_iterations:
            state.metadata["iteration"] += 1

            if current not in self._nodes:
                raise ValueError(f"未找到节点: {current}")

            node = self._nodes[current]
            updates = await node.execute(state)

            # 应用状态更新
            if "messages" in updates:
                new_msgs = updates["messages"]
                removes = [m for m in new_msgs if isinstance(m, RemoveMessage)]
                if removes:
                    state.remove_messages([m.id for m in removes if m.id])
                additions = [m for m in new_msgs if not isinstance(m, RemoveMessage)]
                state.add_messages(additions)
                self._mark_pending_persist(state, additions)

            if "summary" in updates:
                state.summary = updates["summary"]
            if "metadata" in updates:
                state.metadata.update(updates["metadata"])

            edge = self._find_edge(current)
            if edge is None:
                break

            next_node = edge.resolve(state)
            visited_edges.append(f"{current}->{next_node}")
            current = next_node

        state.metadata["visited_edges"] = visited_edges

        # 持久化到数据库
        self._persist_state(thread_id, state)

        return state

    async def astream(
        self,
        user_input: str,
        thread_id: str = "default",
        max_iterations: int = 50,
    ):
        """
        流式执行智能体循环，逐步 yield 每个节点的输出

        关键：状态在Agent级别维护，跨多次astream调用保持对话连贯。
        """
        state = self._get_or_create_state(thread_id)
        user_msg = HumanMessage(content=user_input, id=f"msg_{uuid.uuid4().hex[:12]}")
        state.add_message(user_msg)
        self._mark_pending_persist(state, [user_msg])
        state.metadata["iteration"] = 0

        current = "agent"
        visited_edges: list[str] = []

        while current != "__end__" and state.metadata["iteration"] < max_iterations:
            state.metadata["iteration"] += 1

            if current not in self._nodes:
                break

            node = self._nodes[current]
            updates = await node.execute(state)

            # 应用状态更新
            if "messages" in updates:
                new_msgs = updates["messages"]
                removes = [m for m in new_msgs if isinstance(m, RemoveMessage)]
                if removes:
                    state.remove_messages([m.id for m in removes if m.id])
                additions = [m for m in new_msgs if not isinstance(m, RemoveMessage)]
                state.add_messages(additions)
                self._mark_pending_persist(state, additions)
            if "summary" in updates:
                state.summary = updates["summary"]
            if "metadata" in updates:
                state.metadata.update(updates["metadata"])

            yield {current: updates}

            edge = self._find_edge(current)
            if edge is None:
                break

            next_node = edge.resolve(state)
            visited_edges.append(f"{current}->{next_node}")
            current = next_node

        # 流式执行结束后持久化
        state.metadata["visited_edges"] = visited_edges
        self._persist_state(thread_id, state)

    def clear_conversation(self, thread_id: str) -> None:
        """清除指定会话的所有数据（内存+数据库）"""
        self._states.pop(thread_id, None)
        self._persisted_counts.pop(thread_id, None)
        if self._store:
            self._store.clear_thread(thread_id)
