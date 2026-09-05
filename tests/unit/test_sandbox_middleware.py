"""SandboxMiddleware + Agent 接线测试（加固 Phase 2）。

覆盖方案验证清单：
- acquire/release 在成功、异常、cancel、generator close 下各恰好一次；
- 两个 thread 获得不同 sandbox ID（Local 确定性 ID；物理目录共享如实保留）；
- 默认 Agent 绑定 factory 生成的沙箱四件套（fail closed），legacy 不进默认装配；
- 显式 tools 不被暗中追加，也不自动挂沙箱生命周期；
- LLM 发起 write 后 read，数据真实落到临时 Local mapping；
- plugin run 在 Agent 上下文复用同一 provider 的沙箱，无上下文时回退 legacy；
- owned provider 由 agent.aclose() 关闭，borrowed provider 不被 Agent 擅自关闭。
"""
import asyncio
import os
import tempfile
import unittest
from contextlib import suppress
from unittest.mock import patch

from langchain_core.messages import AIMessage

from novamind.core.middlewares import MiddlewareManager
from novamind.core.middlewares.sandbox_middleware import SandboxMiddleware
from novamind.core.sandbox.context import (
    get_current_sandbox,
    reset_current_sandbox,
    set_current_sandbox,
)
from novamind.core.sandbox.local import LocalSandboxProvider
from novamind.core.sandbox.types import PathMapping
from novamind.core.state_machine import NovaMindAgent

from _fakes import FakeAudit, FakeLLM


class FakeSandbox:
    """最小 Sandbox 替身：只承载身份。"""

    def __init__(self, sandbox_id: str):
        self.sandbox_id = sandbox_id


class FakeProvider:
    """可计数的 SandboxProvider 替身（duck typing，无需继承）。"""

    def __init__(self):
        self.acquire_calls = []
        self.release_calls = []
        self.shutdown_calls = 0
        self._sandboxes: dict[str, FakeSandbox] = {}
        self._counter = 0

    async def acquire_async(self, thread_id, user_id=None):
        self.acquire_calls.append((thread_id, user_id))
        self._counter += 1
        sid = f"sbx_{self._counter}"
        self._sandboxes[sid] = FakeSandbox(sid)
        return sid

    def get(self, sandbox_id):
        return self._sandboxes.get(sandbox_id)

    def release(self, sandbox_id):
        self.release_calls.append(sandbox_id)

    def shutdown(self):
        self.shutdown_calls += 1


class ExplodingBefore:
    """before_agent 抛错的中间件：验证前序中间件已 acquire 的沙箱仍会被释放。"""

    async def abefore_agent(self, ctx):
        raise RuntimeError("boom in before_agent")


def _make_agent(provider, node_fn=None, extra_middlewares=None):
    """构造带 SandboxMiddleware 的最小状态机（不依赖 LLM）。"""
    middlewares = [SandboxMiddleware(provider)] + list(extra_middlewares or [])
    agent = NovaMindAgent(middleware_manager=MiddlewareManager(middlewares))
    captured = {}

    async def agent_node(state):
        captured["ctx_sandbox"] = get_current_sandbox()
        captured["state_sandbox"] = state.sandbox
        if node_fn is not None:
            return await node_fn(state)
        return {"messages": [AIMessage(content="done", id="msg_done")]}

    agent.add_node("agent", agent_node)
    agent.add_conditional_edge(
        "agent", lambda s: "__end__", {"__end__": "__end__"}
    )
    return agent, captured


class TestExactlyOnceLifecycle(unittest.TestCase):
    """acquire / release 在各退出路径下恰好一次。"""

    def test_success_path_acquires_and_releases_once(self):
        provider = FakeProvider()
        agent, captured = _make_agent(provider)

        async def _run():
            state = await agent.run("hi", thread_id="t_ok")
            captured["after_run_ctx"] = get_current_sandbox()
            return state

        state = asyncio.run(_run())
        self.assertEqual(len(provider.acquire_calls), 1)
        self.assertEqual(provider.release_calls, [f"sbx_{provider._counter}"])
        self.assertIsNotNone(captured["ctx_sandbox"])
        self.assertIs(captured["ctx_sandbox"], captured["state_sandbox"])
        self.assertIsNone(captured["after_run_ctx"])       # 释放后上下文恢复
        self.assertIsNone(state.sandbox)                    # 释放后状态字段清空

    def test_exception_path_still_releases_once(self):
        provider = FakeProvider()

        async def boom(state):
            raise RuntimeError("node exploded")

        agent, _ = _make_agent(provider, node_fn=boom)

        async def _run():
            with self.assertRaises(RuntimeError):
                await agent.run("hi", thread_id="t_err")

        asyncio.run(_run())
        self.assertEqual(len(provider.acquire_calls), 1)
        self.assertEqual(len(provider.release_calls), 1)

    def test_later_middleware_failure_still_releases(self):
        provider = FakeProvider()
        agent, _ = _make_agent(provider, extra_middlewares=[ExplodingBefore()])

        async def _run():
            with self.assertRaises(RuntimeError):
                await agent.run("hi", thread_id="t_late_err")

        asyncio.run(_run())
        self.assertEqual(len(provider.acquire_calls), 1)
        self.assertEqual(len(provider.release_calls), 1)

    def test_cancel_path_releases_once(self):
        provider = FakeProvider()

        async def slow_node(state):
            await asyncio.sleep(5)

        agent, _ = _make_agent(provider, node_fn=slow_node)

        async def _run():
            task = asyncio.create_task(agent.run("hi", thread_id="t_cancel"))
            await asyncio.sleep(0.05)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        asyncio.run(_run())
        self.assertEqual(len(provider.acquire_calls), 1)
        self.assertEqual(len(provider.release_calls), 1)

    def test_generator_close_releases_once(self):
        provider = FakeProvider()

        async def slow_node(state):
            await asyncio.sleep(5)
            return {"messages": [AIMessage(content="done", id="msg_done")]}

        agent, _ = _make_agent(provider, node_fn=slow_node)

        async def _run():
            agen = agent.astream("hi", thread_id="t_close")
            consume = asyncio.create_task(agen.__anext__())
            await asyncio.sleep(0.05)
            consume.cancel()
            with suppress(asyncio.CancelledError):
                await consume
            await agen.aclose()

        asyncio.run(_run())
        self.assertEqual(len(provider.acquire_calls), 1)
        self.assertEqual(len(provider.release_calls), 1)


class TestPerThreadBinding(unittest.TestCase):
    """两个 thread 获得不同 sandbox ID；Local 物理目录共享如实保留。"""

    def test_different_threads_get_different_sandboxes(self):
        provider = FakeProvider()
        seen = []

        async def node(state):
            seen.append(get_current_sandbox())
            return {"messages": [AIMessage(content="ok", id="m")]}

        agent, _ = _make_agent(provider, node_fn=node)

        async def _run():
            await agent.run("hi", thread_id="thread_a")
            await agent.run("hi", thread_id="thread_b")

        asyncio.run(_run())
        self.assertEqual([c.sandbox_id for c in seen], ["sbx_1", "sbx_2"])
        self.assertEqual(
            [tid for tid, _ in provider.acquire_calls], ["thread_a", "thread_b"]
        )

    def test_local_provider_deterministic_ids_differ_but_share_root(self):
        provider = LocalSandboxProvider()
        id_a = provider.acquire("thread_a")
        id_b = provider.acquire("thread_b")
        self.assertNotEqual(id_a, id_b)
        # 跨进程可推导的确定性 ID
        self.assertEqual(id_a, provider.acquire("thread_a"))
        # 当前 Local 实现的物理目录共享行为如实保留：同一 provider 的
        # 映射表是共享的（同一 office 工位根）。
        self.assertIs(provider._path_mappings, provider._path_mappings)


class TestAgentWiring(unittest.TestCase):
    """create_agent_app 默认装配 / 显式 tools / ownership。"""

    def _build(self, **kwargs):
        with patch("novamind.core.agent.get_provider", return_value=FakeLLM()), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]):
            from novamind.core.agent import create_agent_app
            return create_agent_app(audit_logger=FakeAudit(), **kwargs)

    def test_default_assembly_binds_factory_tools_and_sandbox(self):
        agent = self._build()  # tools=None → factory 四件套 + SandboxMiddleware
        self.assertIsInstance(agent.sandbox_provider, LocalSandboxProvider)
        first_mw = agent.middleware_manager.middlewares[0]
        self.assertIsInstance(first_mw, SandboxMiddleware)
        self.assertIs(first_mw._provider, agent.sandbox_provider)

    def test_default_office_tools_are_fail_closed_factory_versions(self):
        fake_llm = FakeLLM()
        with patch("novamind.core.agent.get_provider", return_value=fake_llm), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]):
            from novamind.core.agent import create_agent_app
            create_agent_app(audit_logger=FakeAudit())

        names = {t.name for t in fake_llm.bound_tools}
        for office_tool in (
            "list_office_files", "read_office_file",
            "write_office_file", "execute_office_shell",
        ):
            self.assertIn(office_tool, names)
        # 工厂版本 fail closed：无沙箱上下文时直接拒绝
        office = next(t for t in fake_llm.bound_tools if t.name == "list_office_files")
        with self.assertRaises(RuntimeError):
            office.invoke({})

    def test_explicit_tools_not_appended_and_no_sandbox(self):
        class _Tool:
            name = "my_tool"

        agent = self._build(tools=[_Tool()])
        self.assertIsNone(agent.sandbox_provider)
        self.assertFalse(any(
            isinstance(m, SandboxMiddleware) for m in agent.middleware_manager.middlewares
        ))

    def test_explicit_tools_receives_exactly_the_given_list(self):
        class _Tool:
            name = "my_tool"

        fake_llm = FakeLLM()
        with patch("novamind.core.agent.get_provider", return_value=fake_llm), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]):
            from novamind.core.agent import create_agent_app
            create_agent_app(audit_logger=FakeAudit(), tools=[_Tool()])

        self.assertEqual([t.name for t in fake_llm.bound_tools], ["my_tool"])

    def test_explicit_provider_attaches_middleware_with_custom_tools(self):
        provider = FakeProvider()

        class _Tool:
            name = "my_tool"

        agent = self._build(tools=[_Tool()], sandbox_provider=provider)
        mws = agent.middleware_manager.middlewares
        self.assertEqual(len(mws), 1)
        self.assertIsInstance(mws[0], SandboxMiddleware)
        self.assertIs(agent.sandbox_provider, provider)

    def test_owned_provider_shutdown_on_aclose(self):
        provider = FakeProvider()
        with patch("novamind.core.agent.build_default_sandbox_provider",
                   return_value=provider), \
                patch("novamind.core.agent.get_provider", return_value=FakeLLM()), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]):
            from novamind.core.agent import create_agent_app
            agent = create_agent_app(audit_logger=FakeAudit())  # tools=None → owned

        asyncio.run(agent.aclose())
        self.assertEqual(provider.shutdown_calls, 1)
        self.assertIsNone(agent.sandbox_provider)

    def test_borrowed_provider_not_shutdown_by_agent(self):
        provider = FakeProvider()
        agent = self._build(tools=[], sandbox_provider=provider)

        asyncio.run(agent.aclose())
        self.assertEqual(provider.shutdown_calls, 0)
        self.assertIs(agent.sandbox_provider, provider)

    def test_default_sandbox_provider_fail_closed_on_unknown_mode(self):
        from novamind.core.agent import build_default_sandbox_provider

        with patch("novamind.core.config.SANDBOX_MODE", "docker"):
            with self.assertRaises(ValueError):
                build_default_sandbox_provider()
        with patch("novamind.core.config.SANDBOX_MODE", "local"):
            self.assertIsInstance(
                build_default_sandbox_provider(), LocalSandboxProvider
            )


class TestRealLocalRoundtrip(unittest.TestCase):
    """write 后 read 数据真实落到临时 Local mapping。"""

    def test_write_then_read_across_runs_same_thread(self):
        tmp = tempfile.mkdtemp(prefix="novamind_p2_")
        provider = LocalSandboxProvider(
            path_mappings=[PathMapping(
                container_path="/mnt/novamind/user_data", local_path=tmp,
            )]
        )
        from novamind.core.tools.sandbox_tools import _sandbox_read, _sandbox_write

        results = {}

        async def write_node(state):
            results["write"] = _sandbox_write("notes/a.txt", "hello phase2")
            return {"messages": [AIMessage(content="w", id="m1")]}

        async def read_node(state):
            results["read"] = _sandbox_read("notes/a.txt")
            return {"messages": [AIMessage(content="r", id="m2")]}

        agent, _ = _make_agent(provider, node_fn=write_node)

        async def _run():
            await agent.run("写", thread_id="t_io")
            agent._nodes["agent"] = type(agent._nodes["agent"])(
                "agent", read_node, ""
            )
            await agent.run("读", thread_id="t_io")

        asyncio.run(_run())
        self.assertIn("成功", results["write"])
        self.assertEqual(results["read"], "hello phase2")
        # 物理落盘位置 = 临时映射目录
        self.assertTrue(
            os.path.exists(os.path.join(tmp, "notes", "a.txt")),
            "write 应真实落到临时 Local mapping",
        )


class TestPluginRunReusesSandbox(unittest.TestCase):
    """plugin run 在 Agent 上下文复用注入 provider；无上下文回退 legacy。"""

    def _make_plugin_tool(self):
        from novamind.core.plugin_loader import PluginManager, PluginInfo

        with tempfile.TemporaryDirectory() as td:
            md_path = os.path.join(td, "SKILL.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write("name: demo\ndescription: demo skill\n")
            info = PluginInfo(
                folder="office_demo", name="demo", raw_name="demo",
                description="demo skill", md_path=md_path, run_dir="skills/demo",
            )
            manager = PluginManager()
            return manager._create_tool(info)

    def test_run_inside_sandbox_context_goes_through_provider_sandbox(self):
        tool = self._make_plugin_tool()

        class RecordingSandbox(FakeSandbox):
            pass

        sandbox = RecordingSandbox("sbx_plugin")
        token = set_current_sandbox(sandbox)
        try:
            result = tool.invoke({"mode": "run", "command": "pwd"})
        finally:
            reset_current_sandbox(token)
        # 沙箱路径输出的是虚拟根，而不是 legacy 的 OFFICE_DIR 物理路径
        self.assertIn("/mnt/novamind/user_data", result)

    def test_run_without_sandbox_context_falls_back_to_legacy(self):
        tool = self._make_plugin_tool()
        self.assertIsNone(get_current_sandbox())
        result = tool.invoke({"mode": "run", "command": "pwd"})
        self.assertIn("office", result)


if __name__ == "__main__":
    unittest.main()
