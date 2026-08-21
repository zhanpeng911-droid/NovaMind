"""
多 Agent P5 测试。

覆盖关键不变量：
  - 共享沙箱：PerSubagentBinder 复用父 sandbox_id（不创建新沙箱）
  - 委派工具：make_specialist_tool / make_subagent_tool 生成 delegate_to_* tool
  - ContextVar：set_current_state / get_current_state roundtrip
  - SubagentSpecialist 适配 SubagentProvider 为 SpecialistAgent
"""
import json
import unittest
from unittest import mock

from novamind.core.multiagent import (
    PerSubagentBinder,
    make_specialist_tool,
    make_subagent_tool,
    set_current_state,
    get_current_state,
    SpecialistError,
)
from novamind.core.multiagent.specialists import SubagentSpecialist
from novamind.core.multiagent.runtimes.pi_runtime import PiRuntime, PiRuntimeConfig
from novamind.core.multiagent.types import (
    SpecialistCapabilities,
    SpecialistCapability,
    SpecialistRawResult,
    SpecialistRequest,
    SubagentResult,
    SubagentRequest,
)


class _MockSpecialist:
    @property
    def name(self):
        return "coder"

    @property
    def capabilities(self):
        return SpecialistCapabilities(capabilities=(SpecialistCapability.CODING,))

    def invoke(self, request: SpecialistRequest):
        self.last_request = request
        return SpecialistRawResult(raw_output="done: " + request.goal)


class _MockSubagentProvider:
    def __init__(self):
        self.last_request = None

    def spawn(self, request: SubagentRequest):
        self.last_request = request
        return SubagentResult(summary="sub done", success=True)


class TestSandboxBinder(unittest.TestCase):
    def test_bind_reuses_sandbox_id(self):
        binder = PerSubagentBinder()
        bound = binder.bind("coder", "sandbox-123")
        self.assertEqual(bound.sandbox_id, "sandbox-123")
        self.assertEqual(bound.specialist_name, "coder")


class TestSpecialistTool(unittest.TestCase):
    def test_generate_and_invoke(self):
        spec = _MockSpecialist()
        tool = make_specialist_tool("coder", spec)
        self.assertEqual(tool.name, "delegate_to_coder")
        set_current_state({"sandbox": {"sandbox_id": "sb-1"}, "messages": []})
        result = tool.invoke({"goal": "write code", "success_criteria": "tests pass"})
        data = json.loads(result)
        self.assertTrue(data["success"])
        self.assertEqual(data["specialist"], "coder")
        # 共享沙箱透传
        self.assertEqual(spec.last_request.sandbox_id, "sb-1")

    def test_specialist_error_returns_json(self):
        class _Failing:
            @property
            def name(self):
                return "bad"

            @property
            def capabilities(self):
                return SpecialistCapabilities()

            def invoke(self, request):
                raise SpecialistError("crash")

        tool = make_specialist_tool("bad", _Failing())
        set_current_state({})
        result = tool.invoke({"goal": "x", "success_criteria": "y"})
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertIn("error", data)


class TestSubagentTool(unittest.TestCase):
    def test_generate_and_invoke(self):
        provider = _MockSubagentProvider()
        tool = make_subagent_tool(provider)
        self.assertEqual(tool.name, "delegate_to_subagent")
        set_current_state({"sandbox": {"sandbox_id": "sb-2"}, "messages": []})
        result = tool.invoke({"goal": "research", "success_criteria": "done"})
        data = json.loads(result)
        self.assertTrue(data["success"])
        self.assertEqual(provider.last_request.sandbox_id, "sb-2")


class TestContextVar(unittest.TestCase):
    def test_roundtrip(self):
        set_current_state({"a": 1})
        self.assertEqual(get_current_state(), {"a": 1})


class TestSubagentSpecialist(unittest.TestCase):
    def test_adapts_provider(self):
        provider = _MockSubagentProvider()
        spec = SubagentSpecialist(provider)
        self.assertEqual(spec.name, "subagent")
        raw = spec.invoke(SpecialistRequest(
            goal="g", success_criteria="s", context_summary="c",
            sandbox_id="sb-3", artifacts_path=None,
        ))
        self.assertEqual(raw.raw_output, "sub done")
        self.assertEqual(provider.last_request.sandbox_id, "sb-3")


class TestPiRuntimePrompt(unittest.TestCase):
    def test_build_prompt_sections(self):
        req = SpecialistRequest(
            goal="fix the bug", success_criteria="tests pass", context_summary="ctx here",
            sandbox_id="sb", artifacts_path=None,
        )
        prompt = PiRuntime._build_prompt(req)
        self.assertIn("fix the bug", prompt)
        self.assertIn("## Context\nctx here", prompt)
        self.assertIn("## Success Criteria\ntests pass", prompt)
        self.assertIn("## What You Did", prompt)
        self.assertIn("## Gaps", prompt)

    def test_build_prompt_no_context(self):
        req = SpecialistRequest(goal="g", success_criteria="", context_summary="", sandbox_id=None, artifacts_path=None)
        prompt = PiRuntime._build_prompt(req)
        self.assertNotIn("## Context", prompt)
        self.assertNotIn("## Success Criteria", prompt)


class TestPiRuntimeCommand(unittest.TestCase):
    def test_command_never_passes_sandbox_url(self):
        """pi 0.84.x 不支持 --sandbox-url（Unknown option 直接失败），即使有 sandbox_id 也不透传。"""
        runtime = PiRuntime(PiRuntimeConfig())
        req = SpecialistRequest(goal="g", success_criteria="s", context_summary="", sandbox_id="sb", artifacts_path=None)
        cmd = runtime._build_command(req)
        self.assertNotIn("--sandbox-url", cmd)

    def test_command_accepts_list_base(self):
        """command 为 list（[node.exe, cli.js] 绕 npm shim）时按序作基命令。"""
        runtime = PiRuntime(PiRuntimeConfig(
            command=[r"C:\node.exe", r"D:\cli.js"],
            provider="novamind",
        ))
        req = SpecialistRequest(goal="g", success_criteria="s", context_summary="", sandbox_id=None, artifacts_path=None)
        cmd = runtime._build_command(req)
        self.assertEqual(cmd[0], r"C:\node.exe")
        self.assertEqual(cmd[1], r"D:\cli.js")
        self.assertEqual(cmd[2], "-p")

    def test_command_includes_provider_model(self):
        """provider/model 配置时生成 --provider/--model flags（自定义 provider 委派）。"""
        runtime = PiRuntime(PiRuntimeConfig(provider="novamind", model="deepseek-v4-flash"))
        req = SpecialistRequest(goal="g", success_criteria="s", context_summary="", sandbox_id=None, artifacts_path=None)
        cmd = runtime._build_command(req)
        i = cmd.index("--provider")
        self.assertEqual(cmd[i + 1], "novamind")
        j = cmd.index("--model")
        self.assertEqual(cmd[j + 1], "deepseek-v4-flash")


class TestPiRuntimeInvoke(unittest.TestCase):
    def test_invoke_returns_output(self):
        runtime = PiRuntime(PiRuntimeConfig(command="fake-pi"))
        req = SpecialistRequest(goal="g", success_criteria="s", context_summary="", sandbox_id=None, artifacts_path=None)
        with mock.patch("novamind.core.multiagent.runtimes.pi_runtime.subprocess.run") as run:
            run.return_value = mock.MagicMock(returncode=0, stdout="done\n", stderr="")
            result = runtime.invoke(req)
        self.assertEqual(result.raw_output, "done")
        self.assertGreaterEqual(result.duration_seconds, 0.0)

    def test_invoke_not_found_raises(self):
        runtime = PiRuntime(PiRuntimeConfig(command="missing-pi"))
        req = SpecialistRequest(goal="g", success_criteria="s", context_summary="", sandbox_id=None, artifacts_path=None)
        with mock.patch("novamind.core.multiagent.runtimes.pi_runtime.subprocess.run", side_effect=FileNotFoundError("pi")):
            with self.assertRaises(SpecialistError):
                runtime.invoke(req)

    def test_invoke_nonzero_exit_raises(self):
        runtime = PiRuntime(PiRuntimeConfig(command="pi"))
        req = SpecialistRequest(goal="g", success_criteria="s", context_summary="", sandbox_id=None, artifacts_path=None)
        with mock.patch("novamind.core.multiagent.runtimes.pi_runtime.subprocess.run") as run:
            run.return_value = mock.MagicMock(returncode=1, stdout="", stderr="boom")
            with self.assertRaises(SpecialistError):
                runtime.invoke(req)

    def test_invoke_none_stdout_does_not_crash(self):
        """GBK 解码 UTF-8 失败时 readerthread 静默崩溃，communicate 返回 stdout=None。
        invoke 必须兜底为空串而非 AttributeError('NoneType' has no attribute 'strip')。"""
        runtime = PiRuntime(PiRuntimeConfig(command="pi"))
        req = SpecialistRequest(goal="g", success_criteria="s", context_summary="", sandbox_id=None, artifacts_path=None)
        with mock.patch("novamind.core.multiagent.runtimes.pi_runtime.subprocess.run") as run:
            run.return_value = mock.MagicMock(returncode=0, stdout=None, stderr=None)
            result = runtime.invoke(req)
        self.assertEqual(result.raw_output, "")


if __name__ == "__main__":
    unittest.main()
