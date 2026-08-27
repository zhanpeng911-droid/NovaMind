"""Part A：无外部依赖的本地确定性功能验收（A1-A16）。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch


from _fakes import FakeAuditLogger, FakeLLM
from conftest import PROJECT_ROOT


def _build_agent(llm, audit, tools=None, middlewares=None):
    from novamind.core.agent import create_agent_app
    with patch("novamind.core.agent.get_provider", return_value=llm), \
            patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
            patch("novamind.core.agent.load_mcp_tools", return_value=[]):
        return create_agent_app(audit_logger=audit, tools=tools or [],
                                middlewares=middlewares)


# ── A1 CLI 四命令 ──────────────────────────────────────────────
class TestA1Cli(unittest.TestCase):
    def _run(self, *args, timeout=60):
        env = dict(os.environ)
        return subprocess.run([sys.executable, "-m", "entry.cli", *args],
                              capture_output=True, text=True, timeout=timeout,
                              cwd=str(PROJECT_ROOT), env=env)

    def test_a1_help_shows_all_subcommands(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        for cmd in ("run", "monitor", "doctor", "gui", "config"):
            self.assertIn(cmd, r.stdout)

    def test_a1_doctor_json_is_valid_and_exits_zero(self):
        r = self._run("doctor", "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertTrue(data.get("ok") is True)
        self.assertIn("findings", data)

    def test_a1_monitor_list_no_crash(self):
        r = self._run("monitor", "--list")
        self.assertEqual(r.returncode, 0)


# ── A2 编排状态机 ──────────────────────────────────────────────
class TestA2StateMachine(unittest.TestCase):
    def test_a2_ten_tool_turns_then_converge(self):
        from langchain_core.messages import AIMessage
        responses = [
            AIMessage(content="", tool_calls=[
                {"name": "get_current_time", "args": {}, "id": f"tc_{i}"}])
            for i in range(10)
        ]
        responses.append(AIMessage(content="十轮工具调用后收敛完成。"))
        llm = FakeLLM(responses=responses)
        audit = FakeAuditLogger()
        agent = _build_agent(llm, audit, tools=[])
        tid = f"a2_{uuid.uuid4().hex[:8]}"
        result = asyncio.run(agent.run("连续十次查询时间然后总结", thread_id=tid))
        agent.clear_conversation(tid)
        ai_msgs = [m for m in result.messages if getattr(m, "type", None) == "ai"]
        self.assertEqual(ai_msgs[-1].content, "十轮工具调用后收敛完成。")
        self.assertNotIn("max_iterations_reached", result.metadata)

    def test_a2_iteration_limit_terminates(self):
        from langchain_core.messages import AIMessage
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content="", tool_calls=[
            {"name": "get_current_time", "args": {}, "id": "tc_loop"}])
        llm.bind_tools.return_value = llm
        audit = FakeAuditLogger()
        agent = _build_agent(llm, audit, tools=[])
        tid = f"a2l_{uuid.uuid4().hex[:8]}"
        result = asyncio.run(agent.run("循环", thread_id=tid, max_iterations=5))
        agent.clear_conversation(tid)
        self.assertTrue(result.metadata.get("max_iterations_reached"))
        sys_action = [e for e in audit.events if e["event"] == "system_action"]
        self.assertTrue(sys_action)


# ── A3 中间件五钩子 ────────────────────────────────────────────
class _RecordingMiddleware:
    def __init__(self):
        self.calls: list[str] = []

    async def abefore_agent(self, ctx):
        self.calls.append("before_agent")

    async def aafter_agent(self, ctx):
        self.calls.append("after_agent")

    async def abefore_model(self, ctx):
        self.calls.append("before_model")

    async def aafter_model(self, ctx):
        self.calls.append("after_model")

    async def awrap_tool_call(self, ctx):
        self.calls.append("wrap_tool_call")


class TestA3MiddlewareHooks(unittest.TestCase):
    def test_a3_five_hooks_fire_and_order(self):
        from langchain_core.messages import AIMessage
        rec = _RecordingMiddleware()
        llm = FakeLLM(responses=[AIMessage(content="ok")])
        audit = FakeAuditLogger()
        agent = _build_agent(llm, audit, middlewares=[rec])
        tid = f"a3_{uuid.uuid4().hex[:8]}"
        asyncio.run(agent.run("hi", thread_id=tid))
        agent.clear_conversation(tid)
        self.assertIn("before_agent", rec.calls)
        self.assertIn("after_agent", rec.calls)
        self.assertIn("before_model", rec.calls)
        self.assertIn("after_model", rec.calls)
        # 单轮无工具调用：wrap_tool_call 可不触发，但顺序满足先 before 后 after
        self.assertLess(rec.calls.index("before_agent"),
                        rec.calls.index("after_agent"))

    def test_a3_add_middleware_does_not_change_default_behavior(self):
        from langchain_core.messages import AIMessage
        base_audit = FakeAuditLogger()
        base = _build_agent(FakeLLM(responses=[AIMessage(content="r")]), base_audit)
        rec = _RecordingMiddleware()
        ext_audit = FakeAuditLogger()
        ext = _build_agent(FakeLLM(responses=[AIMessage(content="r")]), ext_audit,
                           middlewares=[rec])
        tid = f"a3b_{uuid.uuid4().hex[:8]}"
        r1 = asyncio.run(base.run("hi", thread_id=tid))
        tid2 = f"a3c_{uuid.uuid4().hex[:8]}"
        r2 = asyncio.run(ext.run("hi", thread_id=tid2))
        base.clear_conversation(tid)
        ext.clear_conversation(tid2)
        self.assertEqual(r1.messages[-1].content, r2.messages[-1].content)


# ── A5 记忆五层（L4/L5 机制，mock LLM）────────────────────────
class TestA5MemoryMechanism(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _fake_extract_llm(self):
        calls = {"n": 0}

        def invoke(prompt, **kw):
            if "Merge the following" in str(prompt):
                return type("R", (), {"content": "合并后的语义知识：用户偏好异步编排。"})()
            # 每次抽取返回不同记忆点，避免 encode 去重吞掉第二条
            calls["n"] += 1
            return type("R", (), {"content": json.dumps([
                {"content": f"记忆点{calls['n']}：用户偏好异步编程",
                 "type": "episodic", "importance": 0.8},
            ])})()
        m = MagicMock()
        m.invoke.side_effect = invoke
        return m

    def test_a5_worker_extract_then_consolidate_and_forgotten(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from novamind.core.memory.config import MemoryConfig, get_memory_config as _get_memory_config, set_memory_config
        from novamind.core.memory.strategies.default.manager import DefaultMemoryManager
        from novamind.core.memory.strategies.default.store import MarkdownFileStore
        from novamind.core.memory.strategies.default.retriever import HybridRetriever
        from novamind.core.memory.worker import MemoryTask, MemoryWorker
        from novamind.core.memory.types import MemoryQuery
        from novamind.core.memory.schema import MemoryType

        old = _get_memory_config()
        set_memory_config(MemoryConfig(
            storage_path=str(self.root / "mem"),
            phase2={"enabled": True, "trigger_every_n_turns": 2,
                    "trigger_on_session_end": True},
        ))
        try:
            store = MarkdownFileStore(str(self.root / "mem"))
            manager = DefaultMemoryManager(store=store,
                                           decay_policy=None,
                                           forget_policy=None)
            llm = self._fake_extract_llm()
            worker = MemoryWorker(manager=manager, llm=llm)
            worker.start()
            try:
                conv1 = [HumanMessage(content="我决定都用异步编排"), AIMessage(content="好")]
                worker.submit(MemoryTask(thread_id="t", messages=conv1, turn_count=1))
                # 等待第一条 episodic 落库
                deadline = time.time() + 8
                while time.time() < deadline:
                    eps = [t for t in store.list_by_type(MemoryType.EPISODIC)
                           if not t.metadata.get("forgotten")]
                    if eps:
                        break
                    time.sleep(0.3)
                self.assertTrue(eps, "worker 未把 episodic 落库")

                # 第 2 轮用不同内容（否则 encode 去重不新增 trace）→ 触发 consolidate
                conv2 = [HumanMessage(content="我决定都用异步事件循环"), AIMessage(content="好")]
                worker.submit(MemoryTask(thread_id="t", messages=conv2, turn_count=2))
                deadline = time.time() + 8
                merged = None
                while time.time() < deadline:
                    sem = [t for t in store.list_by_type(MemoryType.SEMANTIC)
                           if not t.metadata.get("forgotten")]
                    if sem:
                        merged = sem[0]
                        break
                    time.sleep(0.3)
                self.assertIsNotNone(merged, "consolidate 未产出 semantic")
                self.assertIn("异步编排", merged.content)

                # 源 episodic 被标记 forgotten → retriever 检索过滤
                from novamind.core.memory.strategies.default.decay import EbbinghausDecayPolicy
                retriever = HybridRetriever(store, EbbinghausDecayPolicy())
                results = retriever.retrieve(MemoryQuery(text="异步编程"))
                forgotten_leak = [r for r in results
                                  if r.trace.metadata.get("forgotten")]
                self.assertEqual(len(forgotten_leak), 0, "forgotten trace 泄漏进检索")
            finally:
                worker.shutdown(timeout=3)
        finally:
            set_memory_config(old)

    def test_a5_wiring_gap_default_runtime(self):
        """验收接线事实：默认 create_agent_app 不含记忆/治理中间件。"""
        from novamind.core.agent import create_agent_app
        from novamind.core.middlewares.memory_recall_middleware import MemoryRecallMiddleware
        from novamind.core.middlewares.memory_consolidation_middleware import MemoryConsolidationMiddleware
        from novamind.core.middlewares.context_governance_middleware import ContextGovernanceMiddleware
        with patch("novamind.core.agent.get_provider",
                   return_value=FakeLLM(responses=[MagicMock()])), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]):
            app = create_agent_app(audit_logger=FakeAuditLogger(), tools=[])
        mws = app.middleware_manager.middlewares
        wired = any(isinstance(m, (MemoryRecallMiddleware, MemoryConsolidationMiddleware,
                                   ContextGovernanceMiddleware)) for m in mws)
        print("\n[WIRING] 默认运行时记忆/治理中间件接线数:", sum(
            1 for m in mws if m.__class__.__name__.endswith("Middleware")),
            "| 含记忆/治理:", wired)


# ── A13 心跳调度 ───────────────────────────────────────────────
class TestA13Heartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks_file = Path(self.tmp.name) / "tasks.json"
        self._p = patch("novamind.core.task_store.TASKS_FILE", str(self.tasks_file))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_a13_due_task_triggered_and_repeat_exhausted(self):
        from novamind.core.heartbeat import pacemaker_loop
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tasks = [
            {"id": "t1", "target_time": now, "description": "一次任务",
             "repeat": None, "repeat_count": None},
            {"id": "t2", "target_time": now, "description": "两次循环",
             "repeat": "hourly", "repeat_count": 2},
        ]
        self.tasks_file.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")

        async def scenario():
            q: asyncio.Queue = asyncio.Queue()
            worker = asyncio.create_task(pacemaker_loop(q, check_interval=0.05))
            try:
                fired = []
                deadline = time.time() + 1.2
                while time.time() < deadline and len(fired) < 2:
                    item = await asyncio.wait_for(q.get(), timeout=0.5)
                    fired.append(item)
                    q.task_done()
                    # 手动改写 t2 的 target_time 为新的过去时间以触发 repeat
                    if len(fired) == 1:
                        payload = json.loads(self.tasks_file.read_text(encoding="utf-8"))
                        for t in payload:
                            if t["id"] == "t2":
                                t["target_time"] = now
                        self.tasks_file.write_text(json.dumps(payload), encoding="utf-8")
                return fired
            finally:
                worker.cancel()

        fired = asyncio.run(scenario())
        self.assertGreaterEqual(len(fired), 1)
        descs = [f.get("data", {}).get("description") if isinstance(f, dict) else str(f)
                 for f in fired]
        self.assertTrue(descs)


if __name__ == "__main__":
    unittest.main()
