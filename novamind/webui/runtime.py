"""WebRuntime — Web 层共享组件运行时（加固 Phase 5）。

替代 server.py 的模块级 _agent/_history_store/_skill_store/_chat_lock：
- 共享 Agent 与 ConversationStore（懒加载 + 初始化锁）；
- 可配置容量 semaphore（默认 4，NOVAMIND_WEB_MAX_MODELS）；
- active task registry：shutdown 时停止接收新工作，给在飞任务有界等待，
  超时后取消；
- 组件 ownership 与关闭顺序：skill store → memory worker/provider →
  Agent（含其 owned 沙箱 provider）→ history store。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from novamind.core.config import DB_PATH, SKILL_DB_PATH
from novamind.core.state_machine import ConversationStore

logger = logging.getLogger("novamind.webui")


def _capacity_from_env(env_key: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(env_key, str(default))))
    except (TypeError, ValueError):
        return default


class WebRuntime:
    """单进程 Web 运行时：组件所有权集中在一处，关闭顺序确定。"""

    def __init__(self, max_concurrent: int | None = None,
                 shutdown_timeout: float | None = None):
        self._init_lock = asyncio.Lock()
        self._agent: Any = None
        self._history_store: ConversationStore | None = None
        self._skill_store: Any = None
        self.capacity = asyncio.Semaphore(
            max_concurrent or _capacity_from_env("NOVAMIND_WEB_MAX_MODELS", 4)
        )
        self.shutdown_timeout = shutdown_timeout or _capacity_from_env(
            "NOVAMIND_WEB_SHUTDOWN_TIMEOUT", 5
        )
        self._active_tasks: set[asyncio.Task] = set()
        self._closing = False

    # ── 组件访问（懒加载） ───────────────────────────────────────────

    async def get_agent(self) -> Any:
        if self._agent is not None:
            return self._agent
        async with self._init_lock:
            if self._agent is not None:
                return self._agent
            if self._closing:
                raise RuntimeError("server is shutting down")

            def _build() -> Any:
                from novamind.core.agent import create_agent_app
                from novamind.core.middlewares.default_stack import (
                    build_default_middlewares,
                )
                from novamind.core.provider import get_provider
                from novamind.webui.server import _load_env

                provider, model = _load_env()
                llm = get_provider(provider_name=provider, model_name=model)
                return create_agent_app(
                    provider_name=provider, model_name=model,
                    middlewares=build_default_middlewares(llm),
                )

            # Agent 组装含同步 IO（env/.env 读取、目录创建），隔离到线程池
            self._agent = await asyncio.to_thread(_build)
        return self._agent

    def get_history_store(self) -> ConversationStore:
        if self._history_store is None:
            self._history_store = ConversationStore(db_path=DB_PATH)
        return self._history_store

    def get_skill_store(self) -> Any:
        if self._skill_store is None:
            from pathlib import Path

            from novamind.core.skill import SQLiteSkillStore
            import novamind.core.skill as skill_pkg

            store = SQLiteSkillStore(SKILL_DB_PATH)
            builtin_dir = Path(skill_pkg.__file__).parent / "builtin_skills"
            if builtin_dir.exists():
                store.discover([builtin_dir], origin="BUILTIN")
            self._skill_store = store
        return self._skill_store

    def agent_if_ready(self) -> Any:
        """已创建的 Agent；未初始化时返回 None（不触发懒加载）。"""
        return self._agent

    # ── active task registry ─────────────────────────────────────────

    def register_task(self, task: asyncio.Task) -> None:
        self._active_tasks.add(task)

    def unregister_task(self, task: asyncio.Task | None) -> None:
        if task is not None:
            self._active_tasks.discard(task)

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def active_task_count(self) -> int:
        return len(self._active_tasks)

    # ── 关闭顺序 ─────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """停止接收新工作 → 有界等待在飞任务 → 取消 → 依序关闭组件。"""
        self._closing = True

        if self._active_tasks:
            tasks = [t for t in self._active_tasks if not t.done()]
            if tasks:
                done, pending = await asyncio.wait(
                    tasks, timeout=self.shutdown_timeout
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        skill_store, self._skill_store = self._skill_store, None
        if skill_store is not None:
            try:
                skill_store.close()
            except Exception:
                logger.exception("skill store close failed")

        # 记忆 L5 worker 与 provider（默认栈经 bootstrap 全局注册）
        try:
            from novamind.core.memory.bootstrap import (
                shutdown_memory_provider,
                shutdown_memory_worker,
            )

            shutdown_memory_worker()
            shutdown_memory_provider()
        except Exception:
            logger.exception("memory shutdown failed")

        agent, self._agent = self._agent, None
        if agent is not None:
            try:
                await agent.aclose()
            except Exception:
                logger.exception("agent aclose failed")

        history_store, self._history_store = self._history_store, None
        if history_store is not None:
            try:
                history_store.close()
            except Exception:
                logger.exception("history store close failed")
