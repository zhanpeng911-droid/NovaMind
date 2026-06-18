"""
NovaMind 实时监控面板 - 全新设计

基于 Rich 终端UI，全新视觉风格：
  - 深色主题 + 霓虹色彩
  - 卡片式事件展示
  - 实时时间戳和事件流
  - 事件类型图标化

监控的事件类型：
  - LLM_INPUT: 发送给模型的消息数量
  - TOOL_CALL: 工具名称 + 参数
  - TOOL_RESULT: 执行结果摘要
  - TOKEN_USAGE: Token 用量 + 成本估算
  - AI_MESSAGE: 模型的回复
  - SYSTEM: 内部状态变更

用法：
  novamind monitor                    # 监控最近一次会话
  novamind monitor --thread-id xxx    # 监控指定会话
  novamind monitor --list             # 列出可监控的会话
"""
import time
import json
import os
from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.table import Table
from rich import box
from datetime import datetime


# 全新监控主题
monitor_theme = Theme({
    "info": "dim cyan",
    "warning": "bold #f0c674",
    "error": "bold #cc6666",
    "llm_input": "dim #8abeb7",
    "tool_call": "bold #b5bd68",
    "tool_result": "bold #81a2be",
    "token_usage": "bold #f0c674",
    "ai_message": "bold #c9b1d9",
    "timestamp": "dim #969696",
    "brand": "bold #81a2be",
})

console = Console(theme=monitor_theme)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")


def _safe_id_from_thread_id(thread_id: str) -> str:
    """将 thread_id 转换为日志文件名（与 logger.py 逻辑一致）"""
    return "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"


def list_sessions() -> list[dict]:
    """
    扫描 logs 目录，返回所有可监控的会话列表。

    每个元素包含：
      - thread_id: 从文件名逆推的原始 thread_id
      - log_path: 日志文件完整路径
      - last_modified: 文件最后修改时间（用于排序）
      - size_bytes: 文件大小
    """
    if not os.path.isdir(LOG_DIR):
        return []

    sessions = []
    for fname in os.listdir(LOG_DIR):
        if not fname.endswith(".jsonl"):
            continue
        log_path = os.path.join(LOG_DIR, fname)
        if not os.path.isfile(log_path):
            continue
        stat = os.stat(log_path)
        # 文件名就是 safe_id，直接用作 thread_id 显示
        thread_id = fname[:-6]  # 去掉 .jsonl 后缀
        sessions.append({
            "thread_id": thread_id,
            "log_path": log_path,
            "last_modified": stat.st_mtime,
            "size_bytes": stat.st_size,
        })

    # 按最后修改时间降序排列（最近的在前）
    sessions.sort(key=lambda s: s["last_modified"], reverse=True)
    return sessions


def resolve_log_target(thread_id: str | None = None) -> str | None:
    """
    解析监控目标日志文件路径。

    Args:
        thread_id: 指定的会话 ID。为 None 时自动选择最近会话。

    Returns:
        日志文件路径，或 None（无可用会话时）。
    """
    if thread_id:
        safe_id = _safe_id_from_thread_id(thread_id)
        log_path = os.path.join(LOG_DIR, f"{safe_id}.jsonl")
        return log_path if os.path.isfile(log_path) else None

    # 未指定 thread_id：选择最近修改的日志文件
    sessions = list_sessions()
    if not sessions:
        return None
    return sessions[0]["log_path"]


def print_header(target_label: str = ""):
    """渲染NovaMind监控面板 - 全新设计"""
    # 顶部品牌区
    brand = Text()
    brand.append("  NOVA", style="bold #81a2be")
    brand.append("MIND", style="bold #b5bd68")
    brand.append("  ::  OBSERVER", style="dim #969696")

    # 状态信息表
    status_table = Table(show_header=False, box=None, padding=(0, 2))
    status_table.add_column("key", style="dim #969696")
    status_table.add_column("value", style="#c5c8c6")
    status_table.add_row("Mode", "Real-time Stream")
    status_table.add_row("Target", target_label or "Agent Events")
    status_table.add_row("Filter", "All")

    content = Text(justify="center")
    content.append("\n")
    content.append(brand)
    content.append("\n\n")
    content.append("  Live Event Stream\n", style="dim #969696 italic")
    content.append("\n")

    panel = Panel(
        Align.center(content),
        title="[bold #81a2be] NovaMind Monitor [/bold #81a2be]",
        title_align="left",
        border_style="#81a2be",
        box=box.DOUBLE_EDGE,
        width=50,
        padding=0,
    )

    console.print(Align.center(panel))
    console.print()
    console.print(status_table)
    console.print()


def tail_f(filepath: str, target_label: str = ""):
    """文件末尾监听（类似 tail -f）"""
    if not os.path.exists(filepath):
        console.print(f"[warning] Waiting for log file: {os.path.basename(filepath)}...[/warning]")
        while not os.path.exists(filepath):
            time.sleep(0.5)

    with open(filepath, "r", encoding="utf-8") as f:
        f.seek(0, 2)
        print_header(target_label)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            yield line


def render_event(line: str):
    """解析并渲染监控日志 - 全新卡片式设计"""
    try:
        data = json.loads(line.strip())
        event = data.get("event")
        ts_str = data.get("ts", "")

        # 解析时间戳
        try:
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            dt_local = datetime.fromisoformat(ts_str).astimezone()
            ts = dt_local.strftime("%H:%M:%S.%f")[:-3]
        except Exception:
            ts = ts_str.split("T")[-1][:12]

        # 事件类型图标
        ICONS = {
            "llm_input": "  ",
            "tool_call": "  ",
            "tool_result": "  ",
            "token_usage": "  ",
            "ai_message": "  ",
            "system_action": "  ",
        }
        icon = ICONS.get(event, "  ")

        if event == "llm_input":
            count = data.get("message_count", 0)
            # 紧凑格式：一行展示
            console.print(
                f"  [timestamp]{ts}[/timestamp] "
                f"[llm_input]{icon} NEURON_WAKE  "
                f"{count} messages sent to model[/llm_input]"
            )

        elif event == "tool_call":
            tool_name = data.get("tool", "unknown")
            args_str = json.dumps(data.get("args", {}), ensure_ascii=False)
            # 如果参数太长则截断
            if len(args_str) > 120:
                args_str = args_str[:117] + "..."
            console.print(
                f"  [timestamp]{ts}[/timestamp] "
                f"[tool_call]{icon} TOOL_CALL    "
                f"{tool_name}[/tool_call] "
                f"[dim]{args_str}[/dim]"
            )

        elif event == "tool_result":
            tool_name = data.get("tool", "unknown")
            result = data.get("result_summary", "")
            display_result = (
                result[:200] + "..." if len(result) > 200 else result
            )
            # 替换换行为空格，保持单行展示
            display_result = display_result.replace("\n", " ")
            console.print(
                f"  [timestamp]{ts}[/timestamp] "
                f"[tool_result]{icon} TOOL_RESULT  "
                f"{tool_name}[/tool_result] "
                f"[dim]{display_result}[/dim]"
            )

        elif event == "token_usage":
            prompt_tokens = int(data.get("prompt_tokens", 0) or 0)
            completion_tokens = int(data.get("completion_tokens", 0) or 0)
            total_tokens = int(data.get("total_tokens", 0) or 0)
            cost = float(data.get("estimated_cost_usd", 0.0) or 0.0)
            model = data.get("model", "unknown")
            console.print(
                f"  [timestamp]{ts}[/timestamp] "
                f"[token_usage]{icon} TOKEN_USAGE "
                f"{model}[/token_usage] "
                f"[dim]in={prompt_tokens:,} out={completion_tokens:,} "
                f"total={total_tokens:,} cost=${cost:.6f}[/dim]"
            )

        elif event == "ai_message":
            content = data.get("content", "")
            # AI回复用面板展示
            display_content = content[:500] + "..." if len(content) > 500 else content
            console.print(Panel(
                f"[ai_message]{display_content}[/ai_message]",
                title=f"[dim]{icon} RESPONSE @ {ts}[/dim]",
                title_align="left",
                border_style="#c9b1d9",
                width=60,
                padding=(0, 1),
            ))

        elif event == "system_action":
            action = data.get("content", "")
            console.print(
                f"  [timestamp]{ts}[/timestamp] "
                f"[warning]{icon} SYSTEM      {action}[/warning]"
            )

    except Exception:
        pass


def main(thread_id: str | None = None, list_sessions_mode: bool = False):
    """
    启动监控面板。

    Args:
        thread_id: 指定监控的会话 ID。为 None 时自动选择最近会话。
        list_sessions_mode: 为 True 时列出可监控会话并退出。
    """
    if list_sessions_mode:
        sessions = list_sessions()
        if not sessions:
            console.print("[warning] 没有找到任何会话日志。[/warning]")
            console.print("[dim]请先运行 novamind run 产生日志。[/dim]")
            return
        table = Table(title="可监控的会话", box=box.ROUNDED)
        table.add_column("Thread ID", style="bold #81a2be")
        table.add_column("大小", style="#c5c8c6")
        table.add_column("最后活动", style="#c5c8c6")
        for s in sessions:
            size_kb = s["size_bytes"] / 1024
            mtime = datetime.fromtimestamp(s["last_modified"]).strftime("%Y-%m-%d %H:%M:%S")
            table.add_row(s["thread_id"], f"{size_kb:.1f} KB", mtime)
        console.print(table)
        return

    target = resolve_log_target(thread_id)
    if target is None:
        if thread_id:
            console.print(f"[error] 未找到会话 '{thread_id}' 的日志文件。[/error]")
            console.print("[dim]使用 novamind monitor --list 查看可用会话。[/dim]")
        else:
            console.print("[warning] 没有找到任何会话日志。[/warning]")
            console.print("[dim]请先运行 novamind run 产生日志。[/dim]")
        return

    target_label = os.path.basename(target).replace(".jsonl", "")
    try:
        console.clear()
        for line in tail_f(target, target_label):
            render_event(line)
    except KeyboardInterrupt:
        console.print("\n[warning]  Monitor disconnected.[/warning]")


if __name__ == "__main__":
    main()
