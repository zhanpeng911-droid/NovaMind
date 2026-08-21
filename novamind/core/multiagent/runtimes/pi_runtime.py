"""PiRuntime — 外部 pi coding agent 委派运行时。

吸收 Poirot `multiagent/runtimes/pi_runtime.py` 的语义，但适配 NovaMind 的 docker exec
沙箱（--sandbox-url 透传容器名，而非 Poirot 的 MCP bridge）：

- specialist 黑盒：invoke 只调 runtime，pi CLI 自管 ReAct loop + 自带 model
- 每次 invoke 启动新 pi 子进程 + 完成关闭（不做 pool）
- 完整 prompt：goal + context_summary + success_criteria + 三段输出格式
- 凭证 env 透传（国内 provider 优先）
- 错误归一化：未安装 / 超时 / 非零退出码
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass

from ..exceptions import SpecialistError, SpecialistTimeoutError
from ..types import SpecialistRawResult, SpecialistRequest


@dataclass(frozen=True)
class PiRuntimeConfig:
    """Pi runtime 配置。provider/model/thinking_level 留空时用 pi 默认。

    command 支持两种形态：
    - str：可执行名/路径（如 "pi" 或 shim 完整路径）
    - list[str]：基命令序列（如 ["node.exe", "cli.js"]，绕过 npm .cmd shim）
    """

    command: str | list[str] = "pi"
    provider: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    extra_args: tuple[str, ...] = ()


class PiRuntime:
    """pi coding agent runtime（subprocess 委派）。

    每次 invoke 启动新 pi 子进程（-p 模式一次性输出）+ 完成关闭。
    注意：pi 0.84.x 不支持 --sandbox-url（Unknown option），勿透传沙箱参数。
    """

    def __init__(self, config: PiRuntimeConfig | None = None) -> None:
        self._config = config or PiRuntimeConfig()

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        start = time.time()
        cmd = self._build_command(request)
        env = self._build_env()

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=request.timeout_seconds, env=env,
                # pi 输出 UTF-8（含中文）。text=True 默认用 locale 编码：windowed exe
                # 无控制台时 locale 是 GBK，解码 UTF-8 中文失败会让 subprocess 的
                # readerthread 静默崩溃、communicate 返回 stdout=None（NoneType.strip 崩溃的根因）。
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,
                # windowed exe（console=False）下阻止子进程弹黑框控制台
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except FileNotFoundError as exc:
            raise SpecialistError(
                f"pi command not found: {self._config.command}. "
                "Install: npm install -g @earendil-works/pi-coding-agent; "
                "若已安装仍报此错（Windows npm 生成的是 pi.cmd 而非 pi.exe），"
                "用 NOVAMIND_PI_COMMAND 指向完整路径，如 D:\\npm-global\\pi.cmd"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SpecialistTimeoutError(timeout_seconds=request.timeout_seconds) from exc

        # 防御：capture_output+text 下 stdout/stderr 理论上恒为 str，但 readerthread
        # 解码崩溃时 communicate 会返回 None（见上方 encoding 注释），兜底为空串。
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""

        if result.returncode != 0:
            raise SpecialistError(
                f"pi exited with code {result.returncode}: {stderr.strip()[:500] or stdout[:500]}"
            )

        return SpecialistRawResult(
            raw_output=stdout.strip(),
            duration_seconds=time.time() - start,
        )

    def _build_command(self, request: SpecialistRequest) -> list[str]:
        """组装 pi CLI 命令：<base> -p <prompt> + provider/model/thinking flags。

        command 为 list 时（如 [node.exe, cli.js]）直接作基命令序列，
        绕过 npm .cmd shim——shim 经 cmd.exe 展开 %*，多行 prompt 会被撕碎。
        """
        prompt = self._build_prompt(request)
        base = self._config.command
        cmd = list(base) if isinstance(base, (list, tuple)) else [base]
        cmd.extend(["-p", prompt])
        if self._config.provider:
            cmd.extend(["--provider", self._config.provider])
        if self._config.model:
            cmd.extend(["--model", self._config.model])
        if self._config.thinking_level:
            cmd.extend(["--thinking", self._config.thinking_level])
        cmd.extend(self._config.extra_args)
        return cmd

    def _build_env(self) -> dict[str, str] | None:
        """透传凭证 env vars（国内 provider 优先，便宜优先）。"""
        pi_env_vars = [
            "DEEPSEEK_API_KEY",
            "KIMI_API_KEY",
            "MINIMAX_API_KEY",
            "XIAOMI_API_KEY",
            "ZAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "OPENROUTER_API_KEY",
            "GROQ_API_KEY",
            "XAI_API_KEY",
            "MISTRAL_API_KEY",
            "TOGETHER_API_KEY",
        ]
        auth_env: dict[str, str] = {}
        for var in pi_env_vars:
            val = os.getenv(var)
            if val:
                auth_env[var] = val
        pi_dir = os.getenv("PI_CODING_AGENT_DIR")
        if pi_dir:
            auth_env["PI_CODING_AGENT_DIR"] = pi_dir
        if not auth_env:
            return None
        # merge：父进程 env + auth vars 覆盖（保证 PATH/HOME 等基础 env 可用）
        return {**os.environ, **auth_env}

    @staticmethod
    def _build_prompt(request: SpecialistRequest) -> str:
        """构造给 pi 的完整 prompt：goal + context + success_criteria + 三段输出格式。"""
        parts = [request.goal]
        if request.context_summary:
            parts.append(f"\n\n## Context\n{request.context_summary}")
        if request.success_criteria:
            parts.append(f"\n\n## Success Criteria\n{request.success_criteria}")
        parts.append(
            "\n\n## Output Format\n"
            "After completing the task, summarize in three sections:\n"
            "## What You Did\n"
            "- Files changed (paths + brief description)\n"
            "- Commands run\n"
            "## Success\n"
            "- Whether success criteria are met (yes/no/partial)\n"
            "- Evidence (test results, file existence, etc.)\n"
            "## Gaps\n"
            "- Any incomplete parts or blockers\n"
            "- Next steps if incomplete"
        )
        return "".join(parts)
