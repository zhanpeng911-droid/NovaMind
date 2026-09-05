"""
NovaMind 沙盒工具集

提供在受控沙盒环境（office 工位）中的文件和Shell操作。

安全策略：
  1. 路径穿越防护：所有路径都相对于 OFFICE_DIR 解析，拒绝 ../ 等越权访问
  2. Shell命令过滤：正则拦截危险命令（绝对路径、盘符跳转等）
  3. 超时熔断：Shell命令执行超过60秒自动终止
  4. 输出截断：防止超大输出撑爆Token
"""
import os
import re
import shlex
import platform
from .base import novamind_tool
from ..config import OFFICE_DIR

SYS_OS = platform.system()


def _get_safe_path(relative_path: str) -> str:
    """
    安全路径解析

    将模型传入的相对路径转换为绝对路径，并检查是否越界。
    如果模型尝试传入 "../../etc/passwd"，这里会直接拦截。

    Args:
        relative_path: 相对于 office 工位的路径

    Returns:
        解析后的绝对路径

    Raises:
        PermissionError: 路径越界
    """
    base_dir = os.path.abspath(OFFICE_DIR)
    target_path = os.path.abspath(os.path.join(base_dir, relative_path))

    # 核心防御：目标路径必须真实落在 OFFICE_DIR 内，不能只做字符串前缀判断
    if os.path.commonpath([base_dir, target_path]) != base_dir:
        raise PermissionError(
            f"越权拦截：你试图访问沙盒外的路径 '{relative_path}'！"
            "你只能在 office 工位内活动。"
        )

    return target_path


def _list_office_files_impl(sub_dir: str = "") -> str:
    try:
        target_dir = _get_safe_path(sub_dir)
        if not os.path.exists(target_dir):
            return f"目录不存在：{sub_dir}"

        items = os.listdir(target_dir)
        if not items:
            return f"[{sub_dir if sub_dir else 'office 根目录'}] 是空的。"

        result = []
        for item in items:
            item_path = os.path.join(target_dir, item)
            item_type = "📁" if os.path.isdir(item_path) else "📄"
            result.append(f"{item_type} {item}")

        return "\n".join(result)
    except Exception as e:
        return str(e)


def _read_office_file_impl(filepath: str) -> str:
    try:
        target_path = _get_safe_path(filepath)
        if not os.path.exists(target_path):
            return f"文件不存在：{filepath}"

        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 防爆截断：防止读取几个 G 的日志把 Token 撑爆
            if len(content) > 10000:
                return content[:10000] + "\n\n...[内容过长，已被安全截断]..."
            return content
    except Exception as e:
        return str(e)


def _write_office_file_impl(filepath: str, content: str, mode: str = "w") -> str:
    try:
        target_path = _get_safe_path(filepath)

        if mode not in ["w", "a"]:
            return "❌ 错误：mode 参数必须是 'w' (覆盖) 或 'a' (追加)。"

        # 确保父目录存在
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        with open(target_path, mode, encoding="utf-8") as f:
            # 追加模式下自动补换行，防止代码粘连
            if mode == "a" and not content.startswith("\n"):
                f.write("\n" + content)
            else:
                f.write(content)

        action = "覆盖/新建" if mode == "w" else "追加"
        return f" ● 成功以 {action} 模式写入文件：{filepath} (共 {len(content)} 字符)"
    except Exception as e:
        return str(e)


@novamind_tool
def list_office_files(sub_dir: str = "") -> str:
    """
    查看你的 office 工位里有哪些文件和文件夹。
    如果 sub_dir 为空，则查看工位根目录。
    """
    return _list_office_files_impl(sub_dir)


@novamind_tool
def read_office_file(filepath: str) -> str:
    """
    读取 office 工位里指定文件的内容。
    filepath 参数应该是相对于 office 的路径，例如 "test.py" 或 "skills/my_skill.py"。
    """
    return _read_office_file_impl(filepath)


@novamind_tool
def write_office_file(filepath: str, content: str, mode: str = "w") -> str:
    """
    在 office 工位里操作文件内容。

    参数说明:
    - filepath: 相对路径，例如 "spider.py" 或 "docs/readme.md"。
    - content: 要写入的具体文本或代码内容。
    - mode: 写入模式。
        - "w" (默认): 【覆盖/新建】模式。
        - "a": 【追加】模式。保留原内容，将新内容追加到文件最末尾。

    智能体操作规范：
    1. 如果你要修改一个长文件中间的某几行，最安全的做法是：
       读取原文件，在你的内存中完成替换，然后用 "w" 模式重写。
    2. 如果你需要查看文件或目录，请优先使用 read_office_file/list_office_files。
    """
    return _write_office_file_impl(filepath, content, mode)


_SHELL_METACHARS = re.compile(r"[&|;<>`\n\r]")
_ENV_EXPANSION = re.compile(r"(%[^%]+%|\$[A-Za-z_][A-Za-z0-9_]*|\$\(|\$\{)")
_ALLOWED_SHELL_COMMANDS = {"pwd", "echo", "ls", "dir", "cat", "type", "mkdir"}


@novamind_tool
def execute_office_shell(command: str) -> str:
    """
    在 office 工位中执行受控命令。

    环境限制：
    1. 跨平台注意：当前宿主机可能是 Windows、Linux 或 Mac。
    2. 为防止越权，本工具不再执行任意 shell，只支持安全白名单命令。
    3. 支持命令：pwd, echo, ls/dir, cat/type, mkdir。
    4. 所有路径参数都必须位于 office 工位内部。
    """
    try:
        if _SHELL_METACHARS.search(command) or _ENV_EXPANSION.search(command):
            return "❌ 权限拒绝：受控命令不允许 shell 元字符、重定向、管道或环境变量展开。"

        parts = shlex.split(command)
        if not parts:
            return "❌ 执行失败：命令为空。"

        cmd = parts[0].lower()
        args = parts[1:]
        if cmd not in _ALLOWED_SHELL_COMMANDS:
            return (
                "❌ 权限拒绝：该命令不在安全白名单中。"
                "支持：pwd, echo, ls/dir, cat/type, mkdir。"
            )

        output = f" ● 当前系统: {SYS_OS}\n"
        output += f" ● 执行命令: `{command}`\n"

        if cmd == "pwd":
            if args:
                return "❌ pwd 不接受参数。"
            return output + f" ● office 工位: {OFFICE_DIR}"

        if cmd == "echo":
            return output + "\n[STDOUT]\n" + " ".join(args)

        if cmd in {"ls", "dir"}:
            if len(args) > 1:
                return "❌ ls/dir 最多接受一个相对目录参数。"
            return output + "\n[STDOUT]\n" + _list_office_files_impl(args[0] if args else "")

        if cmd in {"cat", "type"}:
            if not args:
                return "❌ cat/type 需要至少一个文件参数。"
            if len(args) > 5:
                return "❌ cat/type 单次最多读取 5 个文件。"
            chunks = [_read_office_file_impl(arg) for arg in args]
            return output + "\n[STDOUT]\n" + "\n\n".join(chunks)

        if cmd == "mkdir":
            if not args:
                return "❌ mkdir 需要至少一个目录参数。"
            created = []
            for arg in args:
                target_dir = _get_safe_path(arg)
                os.makedirs(target_dir, exist_ok=True)
                created.append(arg)
            return output + "\n[STDOUT]\n已创建目录: " + ", ".join(created)

        return "❌ 未处理的受控命令。"
    except Exception as e:
        return f"❌ 执行异常：{str(e)}"


# ==================== Phase 1：provider-backed 工具工厂 ====================
# 四个工具保留与 legacy 相同的名称、参数 schema 与主要行为，但实现改为
# 从当前 Sandbox 上下文取 Sandbox 并调用其公开 API（SandboxMiddleware 负责设置上下文）。
# legacy 模块级工具保留，供直接调用与 plugin_loader 兼容；Agent 默认装配切到工厂工具见 Phase 2。

VIRTUAL_ROOT = "/mnt/novamind/user_data"

# legacy 工具在 tools/__init__ 暴露，这里复用其参数 schema，保证契约一致


def _norm_virtual_path(relative_path: str) -> str:
    """把相对路径标准化为挂载区虚拟路径：反斜杠归一、拒绝绝对根与 .. 穿越。

    返回 'VIRTUAL_ROOT/<normalized>'。Guard 会做二次校验。
    """
    raw = relative_path or ""
    # 绝对根判断在归一化之前（/abs/x、\abs\x 都拒绝）
    if raw.startswith("/") or raw.startswith("\\"):
        raise PermissionError("absolute root rejected")
    cleaned = raw.replace("\\", "/").strip("/")
    if not cleaned:
        return VIRTUAL_ROOT
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PermissionError("path traversal rejected")
    first = parts[0]
    if first.endswith(":"):
        raise PermissionError("absolute root rejected")
    return f"{VIRTUAL_ROOT}/{'/'.join(parts)}"


def _sandbox_list(sub_dir: str = "") -> str:
    from ..sandbox.context import require_current_sandbox

    sb = require_current_sandbox()
    virtual = _norm_virtual_path(sub_dir)
    try:
        entries = sb.list_dir_typed(virtual, max_entries=1000)
    except Exception as e:
        return f"目录不存在：{sub_dir}" if "not found" in str(e).lower() else f"操作失败：{e}"
    if not entries:
        return f"[{sub_dir if sub_dir else 'office 根目录'}] 是空的。"
    lines = [f"{'📁' if is_dir else '📄'} {name}" for name, is_dir in entries]
    return "\n".join(lines)


def _sandbox_read(filepath: str) -> str:
    from ..sandbox.context import require_current_sandbox

    sb = require_current_sandbox()
    virtual = _norm_virtual_path(filepath)
    try:
        content = sb.read_file(virtual)
    except Exception as e:
        return f"文件不存在：{filepath}" if "not found" in str(e).lower() else f"操作失败：{e}"
    if len(content) > 10000:
        return content[:10000] + "\n\n...[内容过长，已被安全截断]..."
    return content


def _sandbox_write(filepath: str, content: str, mode: str = "w") -> str:
    from ..sandbox.context import require_current_sandbox

    if mode not in ("w", "a"):
        return "❌ 错误：mode 参数必须是 'w' (覆盖) 或 'a' (追加)。"
    sb = require_current_sandbox()
    virtual = _norm_virtual_path(filepath)
    try:
        payload = "\n" + content if mode == "a" and not content.startswith("\n") else content
        sb.write_file(virtual, payload, append=(mode == "a"))
    except Exception as e:
        return f"写入失败：{e}"
    action = "覆盖/新建" if mode == "w" else "追加"
    return f" ● 成功以 {action} 模式写入文件：{filepath} (共 {len(content)} 字符)"


def _sandbox_shell(command: str) -> str:
    from ..sandbox.context import require_current_sandbox

    if _SHELL_METACHARS.search(command) or _ENV_EXPANSION.search(command):
        return "❌ 权限拒绝：受控命令不允许 shell 元字符、重定向、管道或环境变量展开。"
    parts = shlex.split(command)
    if not parts:
        return "❌ 执行失败：命令为空。"
    cmd = parts[0].lower()
    args = parts[1:]
    if cmd not in _ALLOWED_SHELL_COMMANDS:
        return "❌ 权限拒绝：该命令不在安全白名单中。支持：pwd, echo, ls/dir, cat/type, mkdir。"

    sb = require_current_sandbox()
    out = f" ● 当前系统: {SYS_OS}\n ● 执行命令: `{command}`\n"
    try:
        if cmd == "pwd":
            if args:
                return "❌ pwd 不接受参数。"
            return out + f" ● office 工位: {VIRTUAL_ROOT}"
        if cmd == "echo":
            return out + "\n[STDOUT]\n" + " ".join(args)
        if cmd in ("ls", "dir"):
            if len(args) > 1:
                return "❌ ls/dir 最多接受一个相对目录参数。"
            return out + "\n[STDOUT]\n" + _sandbox_list(args[0] if args else "")
        if cmd in ("cat", "type"):
            if not args:
                return "❌ cat/type 需要至少一个文件参数。"
            if len(args) > 5:
                return "❌ cat/type 单次最多读取 5 个文件。"
            chunks = [_sandbox_read(a) for a in args]
            return out + "\n[STDOUT]\n" + "\n\n".join(chunks)
        if cmd == "mkdir":
            if not args:
                return "❌ mkdir 需要至少一个目录参数。"
            for a in args:
                sb.make_dir(_norm_virtual_path(a))
            return out + "\n[STDOUT]\n已创建目录: " + ", ".join(args)
    except Exception as e:
        return f"❌ 执行异常：{str(e)}"
    return "❌ 未处理的受控命令。"


def build_sandbox_tools(*, require_context: bool = True) -> list:
    """构造 provider-backed 的四个 office 工具（名称/参数与 legacy 一致）。

    require_context=True：无当前 Sandbox 上下文时 fail closed。
    require_context=False：保留给需要延迟 Local fallback 的 legacy 直调场景。
    """
    from langchain_core.tools import StructuredTool

    def _wrap(fn, legacy_tool):
        def runner(**kwargs):
            if require_context:
                from ..sandbox.context import require_current_sandbox

                require_current_sandbox()  # fail closed：无上下文先抛错
            return fn(**kwargs)

        return StructuredTool.from_function(
            func=runner,
            name=legacy_tool.name,
            description=legacy_tool.description,
            args_schema=legacy_tool.args_schema,
        )

    legacy = {
        "list_office_files": list_office_files,
        "read_office_file": read_office_file,
        "write_office_file": write_office_file,
        "execute_office_shell": execute_office_shell,
    }
    impls = {
        "list_office_files": _sandbox_list,
        "read_office_file": _sandbox_read,
        "write_office_file": _sandbox_write,
        "execute_office_shell": _sandbox_shell,
    }
    return [
        _wrap(impls[name], legacy[name])
        for name in ("list_office_files", "read_office_file", "write_office_file", "execute_office_shell")
    ]
