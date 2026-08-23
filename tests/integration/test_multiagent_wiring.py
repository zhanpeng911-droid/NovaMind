"""
多 Agent 入口接线测试。

覆盖关键不变量：
  - pi CLI 检测：PATH 探测 / NOVAMIND_PI_COMMAND 覆盖 / 未安装返回空列表
  - build_delegate_tools：pi 可用生成 delegate_to_pi
  - create_agent_app 默认工具面（tools=None）注入委派工具 + 自动挂 OrchestrationMiddleware
  - 显式传 tools（非 None）不注入委派工具（向后兼容）
  - 端到端：LLM 发起 delegate_to_* 调用 → 工具执行 → 结果回灌
"""
import asyncio
import os
import unittest
import uuid
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from novamind.core.middlewares.orchestration_middleware import OrchestrationMiddleware
from novamind.core.multiagent.bootstrap import (
    build_delegate_tools,
    resolve_pi_base_command,
    resolve_pi_command,
)
from novamind.core.policy import HarnessPolicy
from _fakes import FakeLLM, FakeAudit


@tool("delegate_to_fake")
def _fake_delegate_tool(goal: str, success_criteria: str, sandbox_id: str | None = None) -> str:
    """Delegate to fake specialist for testing."""
    return '{"success": true, "summary": "fake done", "specialist": "fake"}'


class TestResolvePiCommand(unittest.TestCase):
    def test_not_installed_returns_none(self):
        with patch("novamind.core.multiagent.bootstrap.shutil.which", return_value=None):
            self.assertIsNone(resolve_pi_command())

    def test_installed_returns_full_path(self):
        """返回完整路径（含扩展名），而非无扩展名的 "pi"（Windows .cmd shim 坑）。"""
        with patch("novamind.core.multiagent.bootstrap.shutil.which", return_value="/usr/bin/pi"):
            self.assertEqual(resolve_pi_command(), "/usr/bin/pi")

    def test_installed_returns_cmd_shim_path(self):
        """Windows npm 全局安装解析到 pi.cmd 时，返回该完整路径。"""
        with patch("novamind.core.multiagent.bootstrap.shutil.which",
                   return_value=r"D:\npm-global\pi.cmd"):
            self.assertEqual(resolve_pi_command(), r"D:\npm-global\pi.cmd")

    def test_env_override_wins(self):
        with patch.dict(os.environ, {"NOVAMIND_PI_COMMAND": "pi-beta"}):
            self.assertEqual(resolve_pi_command(), "pi-beta")


class TestShimResolution(unittest.TestCase):
    """npm .cmd shim → [node.exe, cli.js]：绕过 cmd.exe，防多行 prompt 被撕碎。"""

    def _make_shim(self, tmpdir: str) -> str:
        shim = os.path.join(tmpdir, "pi.cmd")
        with open(shim, "w", encoding="utf-8") as f:
            f.write(
                '@ECHO off\n'
                'endLocal & "%_prog%"  "%dp0%\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\cli.js" %*\n'
            )
        js = os.path.join(tmpdir, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js")
        os.makedirs(os.path.dirname(js), exist_ok=True)
        open(js, "w").close()
        return shim

    def test_cmd_shim_resolves_to_node_and_js(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            shim = self._make_shim(tmpdir)
            with patch("novamind.core.multiagent.bootstrap.shutil.which",
                       side_effect=lambda name: shim if name == "pi" else r"C:\node.exe"):
                base = resolve_pi_base_command()
        self.assertEqual(base, [r"C:\node.exe", os.path.join(
            tmpdir, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js")])

    def test_exe_path_unchanged(self):
        """非 shim（pi.exe / 显式路径）直接原样返回。"""
        with patch("novamind.core.multiagent.bootstrap.shutil.which",
                   return_value=r"C:\tools\pi.exe"):
            self.assertEqual(resolve_pi_base_command(), r"C:\tools\pi.exe")

    def test_shim_without_node_falls_back(self):
        """shim 存在但 node 不可用时回退原始 shim 路径。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            shim = self._make_shim(tmpdir)
            with patch("novamind.core.multiagent.bootstrap.shutil.which",
                       side_effect=lambda name: shim if name == "pi" else None):
                self.assertEqual(resolve_pi_base_command(), shim)


class TestBuildDelegateTools(unittest.TestCase):
    def test_no_pi_returns_empty(self):
        with patch("novamind.core.multiagent.bootstrap.resolve_pi_command", return_value=None):
            self.assertEqual(build_delegate_tools(), [])

    def test_pi_generates_delegate_tool(self):
        with patch("novamind.core.multiagent.bootstrap.resolve_pi_command", return_value="pi"):
            tools = build_delegate_tools()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "delegate_to_pi")

    def test_provider_model_env_wired(self):
        """NOVAMIND_PI_PROVIDER/MODEL 透传到 PiRuntimeConfig（--provider/--model flags）。"""
        with patch("novamind.core.multiagent.bootstrap.resolve_pi_command", return_value="pi"), \
                patch.dict(os.environ, {
                    "NOVAMIND_PI_PROVIDER": "novamind",
                    "NOVAMIND_PI_MODEL": "deepseek-v4-flash",
                }), \
                patch("novamind.core.multiagent.bootstrap.PiRuntime") as runtime_cls:
            tools = build_delegate_tools()
        self.assertEqual(len(tools), 1)
        cfg = runtime_cls.call_args[0][0]
        self.assertEqual(cfg.command, "pi")
        self.assertEqual(cfg.provider, "novamind")
        self.assertEqual(cfg.model, "deepseek-v4-flash")

    def test_provider_model_default_none(self):
        """未设置环境变量时 provider/model 为 None（用 pi 自身默认）。"""
        with patch("novamind.core.multiagent.bootstrap.resolve_pi_command", return_value="pi"), \
                patch.dict(os.environ, {}, clear=False), \
                patch("novamind.core.multiagent.bootstrap.PiRuntime") as runtime_cls:
            import novamind.core.multiagent.bootstrap as bootstrap
            saved = {k: os.environ.pop(k) for k in ("NOVAMIND_PI_PROVIDER", "NOVAMIND_PI_MODEL") if k in os.environ}
            try:
                build_delegate_tools()
            finally:
                os.environ.update(saved)
        cfg = runtime_cls.call_args[0][0]
        self.assertIsNone(cfg.provider)
        self.assertIsNone(cfg.model)


class TestCreateAgentAppMultiagentWiring(unittest.TestCase):
    def _build(self, tools=None, middlewares=None, llm_responses=None):
        fake_llm = FakeLLM(llm_responses)
        with patch("novamind.core.agent.get_provider", return_value=fake_llm), \
                patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
                patch("novamind.core.agent.load_mcp_tools", return_value=[]), \
                patch("novamind.core.agent.build_delegate_tools", return_value=[_fake_delegate_tool]):
            from novamind.core.agent import create_agent_app
            agent = create_agent_app(audit_logger=FakeAudit(), tools=tools, middlewares=middlewares)
        return agent, fake_llm

    def test_default_tools_wire_delegate_and_orchestration(self):
        """tools=None 时委派工具进工具面，且自动挂 OrchestrationMiddleware。"""
        agent, llm = self._build()
        self.assertTrue(any(t.name == "delegate_to_fake" for t in llm.bound_tools))
        self.assertTrue(any(
            isinstance(mw, OrchestrationMiddleware)
            for mw in agent.middleware_manager.middlewares
        ))

    def test_explicit_tools_skip_delegate(self):
        """显式传 tools（含空列表）不注入委派工具、不挂编排中间件（向后兼容）。"""
        agent, llm = self._build(tools=[])
        self.assertEqual(llm.bound_tools, [])
        self.assertFalse(any(
            isinstance(mw, OrchestrationMiddleware)
            for mw in agent.middleware_manager.middlewares
        ))

    def test_no_duplicate_orchestration_when_caller_provides(self):
        """调用方已传 OrchestrationMiddleware 时不重复挂载。"""
        given = OrchestrationMiddleware()
        agent, _ = self._build(middlewares=[given])
        found = [mw for mw in agent.middleware_manager.middlewares
                 if isinstance(mw, OrchestrationMiddleware)]
        self.assertEqual(len(found), 1)
        self.assertIs(found[0], given)

    def test_delegate_tool_end_to_end(self):
        """端到端：LLM 调 delegate_to_fake → 工具执行 → 结果回灌消息流。"""
        agent, _ = self._build(llm_responses=[
            AIMessage(content="", tool_calls=[{
                "name": "delegate_to_fake",
                "args": {"goal": "write code", "success_criteria": "tests pass"},
                "id": "tc1",
            }]),
            AIMessage(content="已委派完成"),
        ])

        async def _run():
            return await agent.run("帮我委派一个任务", thread_id="maw_" + uuid.uuid4().hex[:6])

        state = asyncio.run(_run())
        tool_msgs = [m for m in state.messages
                     if isinstance(m, ToolMessage) and m.name == "delegate_to_fake"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("fake done", tool_msgs[0].content)
        self.assertTrue(any(m.content == "已委派完成" for m in state.messages
                            if isinstance(m, AIMessage)))


class TestHarnessPolicyDelegatePrefix(unittest.TestCase):
    """策略层前缀放行：delegate_to_* 通过，未知普通工具仍被拦截，前缀移除即关闭。"""

    def test_delegate_tool_allowed_by_prefix(self):
        policy = HarnessPolicy.load(policy_path="__missing__.json")
        self.assertTrue(policy.evaluate_tool_call("delegate_to_pi").allowed)
        self.assertTrue(policy.evaluate_tool_call("delegate_to_coder").allowed)

    def test_unknown_tool_still_blocked(self):
        policy = HarnessPolicy.load(policy_path="__missing__.json")
        decision = policy.evaluate_tool_call("dangerous_tool")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "tool_not_allowed_by_policy")

    def test_prefix_removed_disables_delegation(self):
        policy = HarnessPolicy({
            "default_allowed_tools": ["get_current_time"],
            "delegate_tool_prefixes": [],
        })
        self.assertFalse(policy.evaluate_tool_call("delegate_to_pi").allowed)

    def test_builtin_tool_unaffected(self):
        policy = HarnessPolicy.load(policy_path="__missing__.json")
        self.assertTrue(policy.evaluate_tool_call("get_current_time").allowed)


if __name__ == "__main__":
    unittest.main()
