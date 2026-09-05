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
import copy
import json
import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from langchain_core.messages import (
    BaseMessage, HumanMessage, SystemMessage,
    AIMessage, ToolMessage, RemoveMessage
)
from .logger import AuditLogger
from .token_tracker import TokenTracker
from .context import ContextManager
from .middlewares import MiddlewareContext, MiddlewareManager


@dataclass
class AgentState:
    """
    智能体状态容器

    messages: 完整对话历史（由 add_messages 归约器管理）
    summary: 上下文压缩后的摘要文本
    metadata: 额外元数据（Token用量、轮次计数等）
    sandbox: 运行时字段——当前 run 绑定的 Sandbox 对象（SandboxMiddleware 维护）。
        仅存在于内存状态，不写入 conversation SQLite（持久化只存 messages/summary）。
    """
    messages: list[BaseMessage] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    sandbox: Any = field(default=None, repr=False, compare=False)

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
        # 注意：不能用 with sqlite3.connect(...)——它只管事务提交、不关闭连接，
        # Windows 下会锁住文件导致临时目录清理失败（WinError 32）。
        conn = sqlite3.connect(self._db_path)
        try:
            # Phase 4：并发读写保护——WAL 减少读写互斥，busy_timeout 兜底
            # （Windows/Linux 均适用）；journal_mode 持久化，重复设置幂等。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
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
            # 幂等索引：thread 内按 id 的顺序访问（历史加载/分页）都走它
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_thread_id "
                "ON conversations(thread_id, id)"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _serialize_message(msg: BaseMessage) -> tuple:
        """消息 → SQLite 行参数（role, content, message_type, message_id,
        tool_calls_json, tool_call_id, name）。"""
        role = msg.type if hasattr(msg, "type") else "unknown"
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        message_id = getattr(msg, "id", None)
        name = getattr(msg, "name", None)
        tool_call_id = getattr(msg, "tool_call_id", None)
        tool_calls = None
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_calls = json.dumps(msg.tool_calls, ensure_ascii=False)
        return (role, content, type(msg).__name__,
                message_id, tool_calls, tool_call_id, name)

    @staticmethod
    def _row_to_message(row: tuple) -> BaseMessage | None:
        """SQLite 行（role..name）→ BaseMessage。"""
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
        return msg

    def save_message(self, thread_id: str, msg: BaseMessage) -> None:
        """保存单条消息到数据库"""
        self.save_messages(thread_id, [msg])

    def save_messages(self, thread_id: str, messages: list[BaseMessage]) -> None:
        """批量保存消息（Phase 4：一批一个事务，任一条失败整批回滚）"""
        if not messages:
            return
        with self._lock:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                rows = [self._serialize_message(m) for m in messages]
                conn.executemany(
                    """INSERT INTO conversations
                       (thread_id, role, content, message_type, message_id,
                        tool_calls, tool_call_id, name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(thread_id, *row) for row in rows]
                )
                conn.commit()
            finally:
                conn.close()

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
        """从数据库加载指定会话的完整消息历史（供 Agent 重启恢复，无默认 limit）"""
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
                    msg = self._row_to_message(row)
                    if msg:
                        messages.append(msg)
                return messages
            finally:
                conn.close()

    def load_message_page(
        self,
        thread_id: str,
        limit: int = 50,
        before_id: int | None = None,
    ) -> tuple[list[BaseMessage], int | None]:
        """加载一页消息（返回旧→新顺序），用于持续增长数据的稳定分页。

        - SQL 按 id DESC 取 limit + 1 行，返回前反转为旧到新；
        - 游标是数据库原始行 id（含 tool 行）：下一页条件 id < cursor，
          因此即使整页都是 tool 行也能推进，不会死循环；
        - 返回 (messages, next_cursor)；next_cursor=None 表示没有更旧的一页。
        """
        import sqlite3
        limit = max(1, int(limit))
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                if before_id is None:
                    rows = conn.execute(
                        """SELECT id, role, content, message_type, message_id,
                                  tool_calls, tool_call_id, name
                           FROM conversations
                           WHERE thread_id = ?
                           ORDER BY id DESC LIMIT ?""",
                        (thread_id, limit + 1),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, role, content, message_type, message_id,
                                  tool_calls, tool_call_id, name
                           FROM conversations
                           WHERE thread_id = ? AND id < ?
                           ORDER BY id DESC LIMIT ?""",
                        (thread_id, before_id, limit + 1),
                    ).fetchall()
            finally:
                conn.close()

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1][0] if has_more and rows else None
        rows.reverse()  # 旧 → 新
        messages = [
            msg for msg in (self._row_to_message(row[1:]) for row in rows)
            if msg is not None
        ]
        return messages, next_cursor

    def list_thread_page(
        self,
        limit: int = 20,
        cursor: tuple[int, str] | None = None,
    ) -> tuple[list[dict], tuple[int, str] | None]:
        """按 (last_id, thread_id) 逆序稳定分页列出会话。

        一条 JOIN 查询同时取 count、last timestamp、last id 与首条 human
        标题（消除旧 list_threads 的 N+1）；排序不依赖 timestamp，
        相同时间戳下顺序稳定。cursor=None 从最新一页开始。
        返回 (items, next_cursor)；next_cursor=None 表示没有更多。
        """
        import sqlite3
        limit = max(1, int(limit))
        query = """
            WITH stats AS (
                SELECT thread_id, COUNT(*) AS cnt,
                       MAX(id) AS last_id, MAX(timestamp) AS last_ts
                FROM conversations
                GROUP BY thread_id
            )
            SELECT s.thread_id, s.cnt, s.last_ts, s.last_id,
                   (SELECT content FROM conversations c
                    WHERE c.thread_id = s.thread_id AND c.role = 'human'
                    ORDER BY c.id ASC LIMIT 1) AS first_human
            FROM stats s
        """
        params: list = []
        if cursor is not None:
            last_id, thread_id = cursor
            query += "WHERE (s.last_id < ? OR (s.last_id = ? AND s.thread_id < ?))\n"
            params.extend([last_id, last_id, thread_id])
        query += "ORDER BY s.last_id DESC, s.thread_id DESC LIMIT ?"
        params.append(limit + 1)

        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                rows = conn.execute(query, params).fetchall()
            finally:
                conn.close()

        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for thread_id, cnt, last_ts, _last_id, first_human in rows:
            title = (first_human[:24] if first_human else "新对话")
            items.append({
                "thread_id": thread_id,
                "title": title,
                "message_count": cnt,
                "last_ts": str(last_ts),
            })
        next_cursor = (
            (rows[-1][3], rows[-1][0]) if has_more and rows else None
        )
        return items, next_cursor

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

    def list_threads(self) -> list[dict]:
        """列出所有会话：thread_id + 标题（首条 user 消息）+ 消息数 + 最近时间。"""
        import sqlite3
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                rows = conn.execute(
                    """SELECT thread_id, COUNT(*) as cnt, MAX(timestamp) as last_ts
                       FROM conversations
                       GROUP BY thread_id
                       ORDER BY last_ts DESC"""
                ).fetchall()
                result = []
                for row in rows:
                    thread_id, cnt, last_ts = row
                    first = conn.execute(
                        "SELECT content FROM conversations WHERE thread_id=? AND role='human' ORDER BY id ASC LIMIT 1",
                        (thread_id,),
                    ).fetchone()
                    title = (first[0][:24] if first else "新对话")
                    result.append({
                        "thread_id": thread_id,
                        "title": title,
                        "message_count": cnt,
                        "last_ts": str(last_ts),
                    })
                return result
            finally:
                conn.close()

    def close(self) -> None:
        """显式关闭（当前无持久连接，保留接口 + 触发 gc 释放句柄）。"""
        import gc

        gc.collect()


# 节点函数类型：接收状态，返回状态更新字典
NodeFunc = Callable[[AgentState], Awaitable[dict[str, Any]]]


class _ThreadRunCoordinator:
    """按 thread_id 串行化 run/astream/clear 的注册表（Phase 3）。

    - 同一 thread 的运行/删除互斥（asyncio.Lock），不同 thread 互不阻塞；
    - 引用计数保护锁条目：有在飞/即将进入的 run 时条目保留，
      全部结束后从注册表清除，长驻 WebUI 进程不会随会话数无限增长。
    仅应在事件循环内使用（asyncio.Lock 非线程安全）。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}
        self._registry_lock = threading.Lock()

    def acquire_ref(self, thread_id: str) -> asyncio.Lock:
        """取（或建）该 thread 的锁，并把引用计数 +1。"""
        with self._registry_lock:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            self._refs[thread_id] = self._refs.get(thread_id, 0) + 1
            return lock

    def release_ref(self, thread_id: str) -> None:
        """引用计数 -1；归零时从注册表清除（锁此刻必然空闲）。"""
        with self._registry_lock:
            refs = self._refs.get(thread_id, 1) - 1
            if refs <= 0:
                self._refs.pop(thread_id, None)
                self._locks.pop(thread_id, None)
            else:
                self._refs[thread_id] = refs

    def is_busy(self, thread_id: str) -> bool:
        """该 thread 是否有在飞的 run（同步 clear_conversation 的 idle 检查）。"""
        with self._registry_lock:
            return self._refs.get(thread_id, 0) > 0


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
        middleware_manager: MiddlewareManager | None = None,
        max_cached_states: int = 256,
        sandbox_provider=None,
        owns_sandbox_provider: bool = False,
    ):
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._logger = audit_logger
        self._token_tracker = token_tracker
        self._context_manager = context_manager
        self._store = conversation_store
        self._middleware_manager = middleware_manager or MiddlewareManager()

        # 沙箱生命周期所有权（Phase 2）：
        # - Agent 内部创建的 provider（owns=True）由 aclose() 负责关闭
        # - 外部注入的 provider 由调用者关闭，Agent 不会擅自 shutdown
        self._sandbox_provider = sandbox_provider
        self._owns_sandbox_provider = bool(owns_sandbox_provider)

        # Phase 3：按 thread 串行化运行/删除；运行中的 thread 状态不被 LRU 淘汰
        self._run_coordinator = _ThreadRunCoordinator()
        self._running_threads: set[str] = set()

        # 状态在Agent级别维护：跨run/astream调用保持对话连贯
        # OrderedDict 支持 LRU 淘汰：长驻 webui/GUI 进程防状态字典随会话数无限增长
        self._states: OrderedDict[str, AgentState] = OrderedDict()
        # 持久化计数器：记录每个线程已保存的消息数量，避免全量加载
        self._persisted_counts: dict[str, int] = {}
        self._max_cached_states = max(1, int(max_cached_states))

    @property
    def middleware_manager(self) -> MiddlewareManager:
        return self._middleware_manager

    @property
    def conversation_store(self) -> ConversationStore | None:
        return self._store

    @property
    def sandbox_provider(self):
        return self._sandbox_provider

    async def aclose(self) -> None:
        """释放 Agent 持有的运行时资源。

        仅关闭内部创建（owned）的 sandbox provider；外部注入的 provider
        由调用者负责关闭，这里不碰。"""
        if self._sandbox_provider is not None and self._owns_sandbox_provider:
            provider, self._sandbox_provider = self._sandbox_provider, None
            await asyncio.to_thread(provider.shutdown)

    async def _dispatch_hook(self, hook: str, state: AgentState, thread_id: str) -> None:
        """分发中间件钩子并把结果应用到 state（before/after_agent 用）。"""

        result = await self._middleware_manager.dispatch(
            hook,
            MiddlewareContext(state=state, thread_id=thread_id),
        )
        if result is None or result.is_empty():
            return
        _apply_result(state, result)

    def _get_or_create_state(self, thread_id: str) -> AgentState:
        """
        获取或创建指定线程的状态

        如果状态已存在（同一会话内多次调用），直接复用。
        如果不存在，从数据库加载或创建新状态。
        """
        if thread_id in self._states:
            self._states.move_to_end(thread_id)  # LRU：命中即视为最近使用
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
        self._evict_states_if_needed()
        return state

    def _evict_states_if_needed(self) -> None:
        """LRU 淘汰：状态在每次 run/astream 结束时都已落盘，
        被淘汰的会话下次访问自动从 SQLite 恢复，数据不丢。
        运行中的 thread（Phase 3 pin）不参与淘汰。"""
        while len(self._states) > self._max_cached_states:
            for oldest_tid, _ in self._states.items():
                if oldest_tid not in self._running_threads:
                    self._states.pop(oldest_tid)
                    self._persisted_counts.pop(oldest_tid, None)
                    break
            else:
                break  # 全部是运行中的线程：本轮不淘汰

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
        同一 thread 的并发 run 会串行执行（Phase 3）；不同 thread 互不阻塞。

        Args:
            user_input: 用户输入文本
            thread_id: 会话线程ID（用于日志和状态追踪）
            max_iterations: 最大循环次数（防止无限循环）

        Returns:
            最终的智能体状态
        """
        lock = self._run_coordinator.acquire_ref(thread_id)
        try:
            async with lock:
                return await self._run_turn(user_input, thread_id, max_iterations)
        finally:
            self._run_coordinator.release_ref(thread_id)

    async def _run_turn(
        self,
        user_input: str,
        thread_id: str,
        max_iterations: int,
    ) -> AgentState:
        """单轮循环体（须在 per-thread 锁内执行）。"""
        # 运行 pin 先于状态获取建立：插入本线程状态触发的 LRU 淘汰
        # 也不会淘汰运行中的状态
        self._running_threads.add(thread_id)
        try:
            state = self._get_or_create_state(thread_id)
            return await self._execute_turn(state, user_input, thread_id, max_iterations)
        finally:
            self._running_threads.discard(thread_id)

    async def _execute_turn(
        self,
        state: AgentState,
        user_input: str,
        thread_id: str,
        max_iterations: int,
    ) -> AgentState:
        """轮次主体：快照 → 循环 → 收尾/回滚（状态已获取并 pin）。

        收尾编排是一次性状态机（turn_done / finalized 两个阶段标记）：
        - 轮次本体（before/循环）失败 → 回滚 + finally 补发 after（资源释放）；
        - 收尾阶段（after 钩子 / 持久化）失败 → 回滚本轮，after 不再补发
          （已经执行过或正在失败中），杜绝重复执行收尾钩子。
        """
        snapshot = self._snapshot_state(state)
        counts_before = self._persisted_counts.get(thread_id, 0)
        user_msg = HumanMessage(content=user_input, id=f"msg_{uuid.uuid4().hex[:12]}")
        state.add_message(user_msg)
        self._mark_pending_persist(state, [user_msg])
        state.metadata["iteration"] = 0

        turn_done = False
        finalized = False
        try:
            try:
                # 分发 before_agent 钩子（记忆预载 / 沙箱恢复等横切关注点）。
                # 放在内层 try 内：若后注册的中间件在 before 阶段抛错，
                # 已 acquire 的沙箱仍能经 finally 的 after_agent 释放。
                await self._dispatch_hook("before_agent", state, thread_id)

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

                # 检查是否因达到最大迭代次数而退出
                if state.metadata["iteration"] >= max_iterations:
                    state.metadata["max_iterations_reached"] = True
                    if self._logger:
                        self._logger.log_event(
                            thread_id=thread_id,
                            event="system_action",
                            action=f"达到最大迭代次数 {max_iterations}，智能体循环终止",
                            iteration=state.metadata["iteration"],
                        )

                turn_done = True
            except BaseException:
                # 半轮失败/取消：回滚到轮次开始前（持久化尚未发生）。
                # turn_done 保持 False → finally 补发 after_agent 完成
                # 沙箱等资源释放（恰好一次）。
                self._rollback_state(state, snapshot)
                raise

            # 收尾阶段（turn_done=True）：after 钩子 / 持久化的失败同样
            # 回滚本轮（见外层 except），且 finally 不再补发 after。
            await self._dispatch_hook("after_agent", state, thread_id)

            # 持久化到数据库
            self._persist_state(thread_id, state)
            finalized = True

            return state
        except BaseException:
            if turn_done and not finalized:
                # 收尾失败：回滚本轮（含持久化计数快照，防半程提交后计数超前）
                self._rollback_state(state, snapshot)
                self._persisted_counts[thread_id] = counts_before
            raise
        finally:
            if not turn_done:
                await self._dispatch_hook("after_agent", state, thread_id)

    async def astream(
        self,
        user_input: str,
        thread_id: str = "default",
        max_iterations: int = 50,
    ):
        """
        流式执行智能体循环，逐步 yield 每个节点的输出

        关键：状态在Agent级别维护，跨多次astream调用保持对话连贯。
        同一 thread 的并发 run/astream 会串行执行（Phase 3）；消费者提前
        关闭（GeneratorExit）时半轮回滚、沙箱释放恰好一次。
        """
        lock = self._run_coordinator.acquire_ref(thread_id)
        try:
            async with lock:
                async for event in self._astream_turn(
                    user_input, thread_id, max_iterations
                ):
                    yield event
        finally:
            self._run_coordinator.release_ref(thread_id)

    async def _astream_turn(
        self,
        user_input: str,
        thread_id: str,
        max_iterations: int,
    ):
        """单轮流式循环体（须在 per-thread 锁内执行）。"""
        self._running_threads.add(thread_id)
        try:
            state = self._get_or_create_state(thread_id)
            snapshot = self._snapshot_state(state)
            counts_before = self._persisted_counts.get(thread_id, 0)
            user_msg = HumanMessage(content=user_input, id=f"msg_{uuid.uuid4().hex[:12]}")
            state.add_message(user_msg)
            self._mark_pending_persist(state, [user_msg])
            state.metadata["iteration"] = 0

            # 与 run() 相同的一次性收尾状态机：astream 被提前 close
            # （GeneratorExit）或取消时，finally 补发 after_agent 释放沙箱；
            # 收尾阶段（after 钩子/持久化）失败则回滚且不补发。
            turn_done = False
            finalized = False
            try:
                try:
                    # 分发 before_agent 钩子
                    await self._dispatch_hook("before_agent", state, thread_id)

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

                    # 检查是否因达到最大迭代次数而退出
                    if state.metadata["iteration"] >= max_iterations:
                        state.metadata["max_iterations_reached"] = True
                        if self._logger:
                            self._logger.log_event(
                                thread_id=thread_id,
                                event="system_action",
                                action=f"达到最大迭代次数 {max_iterations}，智能体循环终止",
                                iteration=state.metadata["iteration"],
                            )
                        # 通知消费者：因迭代上限终止
                        yield {"__limit__": {
                            "max_iterations": max_iterations,
                            "iteration": state.metadata["iteration"],
                        }}

                    turn_done = True
                except BaseException:
                    # 半轮失败/消费者提前关闭：回滚到轮次开始前
                    self._rollback_state(state, snapshot)
                    raise

                # 收尾阶段：after 钩子 / 持久化失败同样回滚（见外层 except）
                await self._dispatch_hook("after_agent", state, thread_id)

                # 流式执行结束后持久化
                state.metadata["visited_edges"] = visited_edges
                self._persist_state(thread_id, state)
                finalized = True
            except BaseException:
                if turn_done and not finalized:
                    self._rollback_state(state, snapshot)
                    self._persisted_counts[thread_id] = counts_before
                raise
            finally:
                if not turn_done:
                    await self._dispatch_hook("after_agent", state, thread_id)
        finally:
            self._running_threads.discard(thread_id)

    @staticmethod
    def _snapshot_state(state: AgentState) -> dict[str, Any]:
        """轮次开始前的内存状态快照。

        metadata 做深拷贝：治理等中间件会原地修改嵌套 dict（如
        metadata["governance"]["warned"]），浅拷贝会让取消回滚后残留
        嵌套变更。messages 列表拷贝引用（消息对象视为不可变）。"""
        return {
            "messages": list(state.messages),
            "summary": state.summary,
            "metadata": copy.deepcopy(state.metadata),
        }

    @staticmethod
    def _rollback_state(state: AgentState, snapshot: dict[str, Any]) -> None:
        """半轮失败/取消/收尾失败：把内存状态恢复到轮次开始前。

        metadata 是轮次开始前的深拷贝（含 _pending_persist_messages），
        整体替换即可。持久化计数由调用方按需恢复（收尾失败路径）。"""
        state.messages = list(snapshot["messages"])
        state.summary = snapshot["summary"]
        state.metadata = copy.deepcopy(snapshot["metadata"])

    def clear_conversation(self, thread_id: str) -> None:
        """清除指定会话的所有数据（内存+数据库）。

        仅允许 idle 场景：thread 正在运行时抛错，请改用 aclear_conversation()。"""
        if self._run_coordinator.is_busy(thread_id):
            raise RuntimeError(
                f"thread '{thread_id}' 正在运行中；请使用 aclear_conversation()"
            )
        self._states.pop(thread_id, None)
        self._persisted_counts.pop(thread_id, None)
        if self._store:
            self._store.clear_thread(thread_id)

    async def aclear_conversation(self, thread_id: str) -> None:
        """异步清除指定会话：与 run/astream 按 thread 互斥。

        同一 thread 的在飞轮次结束后才执行删除；不同 thread 不受影响。"""
        lock = self._run_coordinator.acquire_ref(thread_id)
        try:
            async with lock:
                self._states.pop(thread_id, None)
                self._persisted_counts.pop(thread_id, None)
                if self._store:
                    await asyncio.to_thread(self._store.clear_thread, thread_id)
        finally:
            self._run_coordinator.release_ref(thread_id)


def _apply_result(state: AgentState, result) -> None:
    """把 MiddlewareResult 应用到 AgentState（state_patch + messages_patch）。"""

    if result.state_patch:
        for key, value in result.state_patch.items():
            if key == "metadata":
                state.metadata.update(value)
            elif key == "messages":
                state.add_messages([m for m in value if not isinstance(m, RemoveMessage)])
            else:
                setattr(state, key, value)
    if result.messages_patch:
        state.add_messages(result.messages_patch)
