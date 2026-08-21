"""多 Agent 接线 bootstrap — pi CLI 检测 + delegate 工具生成。

create_agent_app 的默认工具面（tools=None）调用 build_delegate_tools()：
- pi CLI 在 PATH（或 NOVAMIND_PI_COMMAND 显式指定）→ 生成 delegate_to_pi 工具
- 未安装 → 返回空列表，工具面不含委派工具（零成本降级，不报错）
"""

from __future__ import annotations

import os
import re
import shutil

from .runtimes.pi_runtime import PiRuntime, PiRuntimeConfig
from .specialists import PiSpecialist
from .tools import make_specialist_tool


def resolve_pi_command() -> str | None:
    """解析 pi CLI 命令：NOVAMIND_PI_COMMAND 显式覆盖优先，否则探测 PATH。

    必须返回 which 解析出的完整路径（含扩展名）：npm 全局安装在 Windows 生成
    pi.cmd shim 而非 pi.exe，无扩展名的 "pi" 会被 CreateProcess 只按 pi.exe
    查找而 FileNotFoundError（PowerShell 能跑但 subprocess 跑不了的根源）。
    """
    override = os.getenv("NOVAMIND_PI_COMMAND")
    if override:
        return override
    return shutil.which("pi")


def _resolve_shim_base(pi_path: str) -> list[str] | None:
    """npm .cmd/.bat shim → [node.exe, cli.js] 基命令。

    Windows npm 全局包只生成 pi.cmd shim（无 pi.exe），shim 经 cmd.exe 展开 %*：
    多行 prompt 会被 cmd 按行拆开执行（第二行被当新命令），导致委派必炸或挂死。
    解析 shim 里的 node_modules JS 入口，直接用 node.exe 调用，绕过 cmd.exe。
    解析失败返回 None（调用方回退原始路径）。
    """
    if not pi_path.lower().endswith((".cmd", ".bat")):
        return None
    try:
        with open(pi_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r'"([^"]*node_modules[^"]*\.js)"', content)
    node_exe = shutil.which("node")
    if not m or not node_exe:
        return None
    js_path = m.group(1).replace("%dp0%", os.path.dirname(pi_path))
    if not os.path.exists(js_path):
        return None
    return [node_exe, js_path]


def resolve_pi_base_command() -> list[str] | str | None:
    """解析 pi 基命令：优先 [node.exe, cli.js]（绕 shim），失败回退原始路径。"""
    command = resolve_pi_command()
    if not command:
        return None
    return _resolve_shim_base(command) or command


def build_delegate_tools() -> list:
    """生成委派工具：pi 可用 → [delegate_to_pi]；不可用 → []。

    NOVAMIND_PI_PROVIDER / NOVAMIND_PI_MODEL 可选指定 pi 的 provider/model，
    配合 ~/.pi/agent/models.json 的自定义 provider 使用（如 OpenAI 兼容中转端点）。
    不指定时用 pi 自身默认（provider=google）。
    """
    command = resolve_pi_base_command()
    if not command:
        return []
    provider = os.getenv("NOVAMIND_PI_PROVIDER") or None
    model = os.getenv("NOVAMIND_PI_MODEL") or None
    runtime = PiRuntime(PiRuntimeConfig(command=command, provider=provider, model=model))
    specialist = PiSpecialist(runtime=runtime)
    return [make_specialist_tool("pi", specialist)]
