"""
NovaMind 主入口模块

负责：
  1. 打印启动横幅（全新ASCII Art设计）
  2. 加载环境配置
  3. 启动智能体循环（用户输入 -> Agent处理 -> 输出展示）
  4. 启动心跳调度器（后台定时任务）
  5. 实时终端UI（Spinner动画、耗时显示）

架构：三个并发协程
  - user_input_loop: 用户输入（prompt_toolkit）
  - agent_worker: 智能体处理（调用LLM + 工具）
  - pacemaker_loop: 心跳调度（定时任务检查）
"""
import os
import time
import asyncio
import random
import secrets
from datetime import datetime

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style
from prompt_toolkit.application import get_app

from novamind.core.agent import create_agent_app
from novamind.core.provider import get_provider
from novamind.core.middlewares.default_stack import build_default_middlewares
from novamind.core.event_bus import event_bus
from novamind.core.heartbeat import pacemaker_loop
from novamind.core.skill.evolution.flag import evolution_notice


# ==================== 终端颜色常量 ====================
# 全新配色方案：深空蓝 + 翡翠绿 + 琥珀金
C_RESET   = "\033[0m"
C_BOLD    = "\033[1m"
C_DIM     = "\033[2m"
C_ITALIC  = "\033[3m"

# 主色调
C_PRIMARY   = "\033[38;5;75m"   # 冰蓝色（品牌主色）
C_ACCENT    = "\033[38;5;114m"  # 翡翠绿（工具调用）
C_WARN      = "\033[38;5;214m"  # 琥珀金（警告/重点）
C_SURFACE   = "\033[38;5;243m"  # 灰色（次要信息）
C_TEXT      = "\033[38;5;252m"  # 浅灰白（正文）
C_USER      = "\033[38;5;141m"  # 紫罗兰（用户输入）
C_AI        = "\033[38;5;123m"  # 天蓝色（AI回复）
C_ERROR     = "\033[38;5;203m"  # 红色（错误）
C_SUCCESS   = "\033[38;5;114m"  # 翡翠绿（成功）


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def type_line(text: str, delay: float = 0.006):
    """逐字打印效果（打字机风格）"""
    for ch in text:
        print(ch, end="", flush=True)
        time.sleep(delay)
    print()


def print_banner():
    """打印NovaMind启动横幅 - 全新设计"""
    clear_screen()

    # 全新ASCII Art：NovaMind 品牌字样
    logo = f"""{C_PRIMARY}{C_BOLD}
    ╔════════════════════════════════════════════════════════════════════════════╗
    ║                                                                            ║
    ║  ███╗   ██╗ ██████╗ ██╗   ██╗ █████╗ ███╗   ███╗██╗███╗   ██╗██████╗    ║
    ║  ████╗  ██║██╔═══██╗██║   ██║██╔══██╗████╗ ████║██║████╗  ██║██╔══██╗   ║
    ║  ██╔██╗ ██║██║   ██║██║   ██║███████║██╔████╔██║██║██╔██╗ ██║██║  ██║   ║
    ║  ██║╚██╗██║██║   ██║╚██╗ ██╔╝██╔══██║██║╚██╔╝██║██║██║╚██╗██║██║  ██║   ║
    ║  ██║ ╚████║╚██████╔╝ ╚████╔╝ ██║  ██║██║ ╚═╝ ██║██║██║ ╚████║██████╔╝   ║
    ║  ╚═╝  ╚═══╝ ╚═════╝   ╚═══╝  ╚═╝  ╚═╝╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚═════╝    ║
    ║                                                                            ║
    ║                         ── 透明 AI 智能体 ──                                ║
    ║                                                                            ║
    ╚════════════════════════════════════════════════════════════════════════════╝{C_RESET}"""

    # 副标题
    tagline = (
        f"  {C_DIM}v2.0{C_RESET}  "
        f"{C_TEXT}每个决策可追踪，每个 Token 可计量。{C_RESET}"
    )

    # 随机技术格言
    quotes = [
        "透明不是选项，而是底线。",
        "默认不信任，始终可验证。",
        "最好的 AI，是你能审计的 AI。",
        "可观测性就是控制力。",
        "零信任，完全可见。",
        "AI 不该是黑箱。",
        "看不见，就无法修正。",
    ]
    quote = random.choice(quotes)
    meta = f"  {C_DIM}>>{C_RESET} {C_ACCENT}{quote}{C_RESET}"

    # 启动提示
    tip = (
        f"  {C_PRIMARY}>>{C_RESET} "
        f"{C_TEXT}NovaMind 已就绪。输入命令开始，{C_USER}/exit{C_TEXT} 退出。{C_RESET}"
    )

    print()
    print(logo)
    print()
    print(tagline)
    print()
    time.sleep(0.1)
    print(meta)
    print()
    type_line(tip, delay=0.003)


def cprint(text="", end="\n"):
    print_formatted_text(ANSI(str(text)), end=end)


def generate_thread_id() -> str:
    """生成唯一的会话 ID：session_YYYYMMDD_HHMMSS_XXXX"""
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(4)
    return f"session_{ts}_{suffix}"


async def async_main(thread_id: str | None = None):
    """NovaMind 异步主循环"""
    print_banner()

    # bootstrap 提醒：技能/多 Agent 进化 L2/L3 默认关闭（决策 3）
    notice = evolution_notice()
    if notice:
        cprint(f"  {C_DIM}ℹ {notice}{C_RESET}")

    from dotenv import load_dotenv
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    load_dotenv(env_path)

    current_provider = os.getenv("DEFAULT_PROVIDER", "openai")
    current_model = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")

    # 缺陷#2 修复：默认 CLI 运行时挂载记忆（L4/L5）与上下文治理中间件
    llm = get_provider(provider_name=current_provider, model_name=current_model)
    agent = create_agent_app(
        provider_name=current_provider,
        model_name=current_model,
        middlewares=build_default_middlewares(llm),
    )

    try:
        await _run_session(agent, thread_id)
    finally:
        # Phase 2 ownership：Agent 内部创建的 sandbox provider 由 aclose 关闭
        await agent.aclose()


async def _run_session(agent, thread_id: str | None):
    """CLI 会话主体（输入循环、agent worker、心跳）。"""
    bus = event_bus

    if not thread_id:
        thread_id = generate_thread_id()

    # 显示当前会话信息
    cprint(f"  {C_DIM}Session: {C_PRIMARY}{thread_id}{C_RESET}")

    # ==================== Spinner动画状态 ====================
    class SpinnerState:
        action_words = [
            "Synthesizing context...",
            "Querying neural pathways...",
            "Processing tokens...",
            "Evaluating options...",
            "Building response graph...",
            "Analyzing intent...",
            "Calibrating output...",
            "Optimizing reasoning...",
            "Mapping knowledge...",
            "Weaving connections...",
        ]
        current_words = []
        is_spinning = False
        start_time = 0.0
        frames = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
        is_tool_calling = False
        tool_msg = ""

    spinner = SpinnerState()

    def get_bottom_toolbar():
        """底部状态栏：显示当前处理状态"""
        if not spinner.is_spinning:
            return ANSI("")

        elapsed = time.time() - spinner.start_time
        if spinner.is_tool_calling:
            display_msg = spinner.tool_msg
        else:
            idx_word = int(elapsed * 0.8) % len(spinner.current_words)
            display_msg = spinner.current_words[idx_word]

        idx_frame = int(elapsed * 10) % len(spinner.frames)
        frame = spinner.frames[idx_frame]

        # 格式化：[ spinner ] 消息 [耗时]
        return ANSI(
            f"  {C_ACCENT}{frame}{C_RESET} "
            f"{C_TEXT}{display_msg}{C_RESET} "
            f"{C_DIM}[{elapsed:.1f}s]{C_RESET}"
        )

    # 输入提示符设计
    prompt_message = ANSI(f"  {C_PRIMARY}>{C_RESET} ")
    placeholder_text = ANSI(f"{C_ITALIC}{C_DIM}输入消息...{C_RESET}")

    async def agent_worker():
        """智能体工作协程：直接从底层队列消费，兼容用户输入和心跳消息"""
        while True:
            user_input = await bus.queue.get()
            bus.queue.task_done()

            if user_input.lower() in ["/exit", "/quit"]:
                break

            spinner.current_words = spinner.action_words.copy()
            random.shuffle(spinner.current_words)
            spinner.start_time = time.time()
            spinner.is_spinning = True
            spinner.is_tool_calling = False

            try:
                async for event in agent.astream(user_input, thread_id=thread_id):
                    for node_name, node_data in event.items():
                        if node_name == "agent":
                            last_msg = (
                                node_data.get("messages", [])[-1]
                                if node_data.get("messages")
                                else None
                            )

                            if last_msg and hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                for tc in last_msg.tool_calls:
                                    spinner.is_tool_calling = True
                                    spinner.tool_msg = f"Executing: {tc['name']}..."
                                    cprint(
                                        f"  {C_ACCENT}>>{C_RESET} "
                                        f"{C_WARN}tool:{C_RESET} {tc['name']}"
                                    )

                            elif last_msg and last_msg.content:
                                spinner.is_spinning = False
                                lines = last_msg.content.strip().split("\n")
                                if lines:
                                    # 输出格式：缩进 + 天蓝色
                                    formatted_out = f"  {C_PRIMARY}>{C_RESET} {C_AI}{lines[0]}"
                                    for line in lines[1:]:
                                        formatted_out += f"\n    {C_DIM}{line}"
                                    formatted_out += C_RESET
                                    cprint(formatted_out)

                        elif node_name == "tools":
                            spinner.is_tool_calling = False

                        elif node_name == "__limit__":
                            spinner.is_spinning = False
                            limit_info = node_data or {}
                            max_iter = limit_info.get("max_iterations", "?")
                            cprint(
                                f"  {C_WARN}⚠️ 已达到最大循环次数 ({max_iter})，"
                                f"Agent 可能未完成全部任务{C_RESET}"
                            )

            except Exception as e:
                spinner.is_spinning = False
                cprint(f"  {C_ERROR}[ ERROR: {e} ]{C_RESET}")

            spinner.is_spinning = False
            cprint()

    async def user_input_loop():
        """用户输入协程"""
        custom_style = Style.from_dict({
            "bottom-toolbar": "bg:default fg:default noreverse",
        })

        session = PromptSession(
            bottom_toolbar=get_bottom_toolbar,
            style=custom_style,
            erase_when_done=True,
            reserve_space_for_menu=0,
        )

        async def redraw_timer():
            while True:
                if spinner.is_spinning:
                    try:
                        get_app().invalidate()
                    except Exception:
                        pass
                await asyncio.sleep(0.08)

        redraw_task = asyncio.create_task(redraw_timer())

        while True:
            try:
                user_input = await session.prompt_async(
                    prompt_message, placeholder=placeholder_text
                )
                user_input = user_input.strip()
                if not user_input:
                    continue

                # 用户输入气泡：深色背景 + 白色文字
                padded_bubble = f"  > {user_input}    "
                cprint(
                    f"\033[48;2;30;30;40m\033[38;5;255m{padded_bubble}\033[0m\n"
                )

                await bus.queue.put(user_input)

                if user_input.lower() in ["/exit", "/quit"]:
                    cprint(
                        f"  {C_PRIMARY}>>{C_RESET} "
                        f"{C_DIM}Session saved. NovaMind entering standby.{C_RESET}"
                    )
                    break

            except (KeyboardInterrupt, EOFError):
                cprint(
                    f"\n  {C_PRIMARY}>>{C_RESET} "
                    f"{C_WARN}Interrupted. NovaMind entering standby.{C_RESET}"
                )
                await bus.queue.put("/exit")
                break

        redraw_task.cancel()

    with patch_stdout():
        worker = asyncio.create_task(agent_worker())
        heartbeat_worker = asyncio.create_task(
            pacemaker_loop(bus.queue, check_interval=10)
        )
        await user_input_loop()
        await bus.queue.join()
        worker.cancel()
        heartbeat_worker.cancel()


def main(thread_id: str | None = None):
    asyncio.run(async_main(thread_id=thread_id))


if __name__ == "__main__":
    main()
