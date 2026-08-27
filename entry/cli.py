"""
NovaMind CLI 入口

提供三个核心命令：
  - novamind config: 交互式配置向导（选择提供商、输入API Key、测试连接）
  - novamind run: 启动智能体主循环
  - novamind monitor: 启动实时监控面板

用法：
  novamind config     # 首次使用，完成模型配置
  novamind run        # 启动智能体
  novamind monitor    # 另开终端，实时监控智能体行为
"""
import os
import sys
import typer
import questionary
import logging
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from dotenv import set_key, load_dotenv, unset_key

from novamind.core.provider import get_provider
from novamind.core.doctor import run_doctor
from langchain_core.messages import HumanMessage

ENTRY_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ENTRY_DIR)

os.chdir(PROJECT_ROOT)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

app = typer.Typer(help="NovaMind - Transparent AI Agent Framework")
console = Console()

# 全新配色方案：冰蓝+翡翠绿
cyber_style = questionary.Style([
    ("qmark", "fg:#81a2be bold"),
    ("question", "fg:#b5bd68 bold"),
    ("answer", "fg:#81a2be bold"),
    ("pointer", "fg:#b5bd68 bold"),
    ("highlighted", "fg:#b5bd68 bold"),
    ("selected", "fg:#81a2be"),
    ("instruction", "fg:#808080 dim"),
])

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")


@app.command("config")
def config_wizard():
    """交互式配置向导"""
    console.clear()
    console.print(Panel(
        " Welcome to [bold #81a2be]NovaMind[/bold #81a2be]...\n\n"
        "[dim] 请完成模型配置，我们将把密钥安全固化在本地。[/dim]",
        title="[bold white]* NovaMind Config[/bold white]",
        border_style="#81a2be",
    ))

    provider_raw = questionary.select(
        "选择你的模型提供商 (Provider):",
        choices=[
            "openai",
            "anthropic",
            "aliyun (openai compatible)",
            "tencent (openai compatible)",
            "z.ai (openai compatible)",
            "other (openai compatible)",
            "ollama",
        ],
        style=cyber_style,
        instruction="(按上下键选择，回车确认)",
    ).ask()

    if not provider_raw:
        console.print("[dim #81a2be]*   录入中断，NovaMind 配置已取消。[/dim #81a2be]")
        return

    provider = provider_raw.split(" ")[0].strip()
    is_openai_compatible = "openai" in provider_raw.lower()

    model_name = questionary.text(
        "输入指定的模型型号 (如 gpt-4o-mini, qwen-max, glm-4 等):",
        style=cyber_style,
    ).ask()

    if model_name is None:
        console.print("[dim #81a2be]*   录入中断，NovaMind 配置已取消。[/dim #81a2be]")
        return

    api_key = ""
    env_key = ""
    if provider != "ollama":
        if is_openai_compatible:
            env_key = "OPENAI_API_KEY"
        elif provider == "anthropic":
            env_key = "ANTHROPIC_API_KEY"

        api_key = questionary.password(
            f"输入你的 {env_key} (对应 {provider_raw}):",
            style=cyber_style,
        ).ask()

        if api_key is None:
            console.print("[dim #81a2be]*   录入中断，NovaMind 配置已取消。[/dim #81a2be]")
            return

    base_url = ""
    if provider in ["openai", "anthropic"]:
        base_url = questionary.text(
            f"输入 {provider} 代理 Base URL (直连请直接回车跳过):",
            style=cyber_style,
        ).ask()
    elif provider == "ollama":
        base_url = questionary.text(
            "输入 Ollama Base URL (默认 http://localhost:11434，直接回车跳过):",
            style=cyber_style,
        ).ask()
    else:
        base_url = questionary.text(
            "输入兼容 Base URL (不填直接回车将使用官方默认地址):",
            style=cyber_style,
        ).ask()

    if base_url is None:
        console.print("[dim #81a2be]*   录入中断，NovaMind 配置已取消。[/dim #81a2be]")
        return

    console.print("\n[dim]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/dim]")

    with Status(
        f"[bold #81a2be]正在连接 {provider.upper()} 引擎并发送探测包...[/bold #81a2be]",
        spinner="dots",
        spinner_style="#b5bd68",
    ):
        try:
            if env_key and api_key:
                os.environ[env_key] = api_key
            if base_url:
                if is_openai_compatible:
                    os.environ["OPENAI_API_BASE"] = base_url
                else:
                    os.environ[f"{provider.upper()}_BASE_URL"] = base_url

            llm = get_provider(provider_name=provider, model_name=model_name)
            llm.invoke([HumanMessage(content="回复我'收到'。")])

            console.print(" [bold #b5bd68][ 配置成功!][/bold #b5bd68]")

        except Exception as e:
            console.print(
                f" [bold #81a2be][ 配置失败!][/bold #81a2be]  "
                f"无法连接到模型，请检查 Key、Base URL、模型型号 或 网络！\n"
                f"[dim]错误信息: {str(e)}[/dim]"
            )
            return

    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, "w").close()

    logging.getLogger("dotenv.main").setLevel(logging.ERROR)

    unset_key(ENV_PATH, "OPENAI_API_BASE")
    unset_key(ENV_PATH, "ANTHROPIC_BASE_URL")
    unset_key(ENV_PATH, "OLLAMA_BASE_URL")

    if env_key and api_key:
        set_key(ENV_PATH, env_key, api_key)

    if base_url:
        if is_openai_compatible:
            set_key(ENV_PATH, "OPENAI_API_BASE", base_url)
        else:
            set_key(ENV_PATH, f"{provider.upper()}_BASE_URL", base_url)

    set_key(ENV_PATH, "DEFAULT_PROVIDER", provider)
    set_key(ENV_PATH, "DEFAULT_MODEL", model_name)

    console.print(Panel(
        f"配置已保存至 [#81a2be]{ENV_PATH}[/#81a2be]\n"
        f"当前默认提供商: [#81a2be]{provider}[/#81a2be] | "
        f"模型: [#81a2be]{model_name}[/#81a2be]\n\n"
        f"输入 [bold #b5bd68]novamind run[/bold #b5bd68] 即可启动系统！",
        border_style="#b5bd68",
    ))


def _show_boot_error():
    console.print(Panel(
        "[bold #b5bd68]NovaMind 未完成配置![/bold #b5bd68]\n\n"
        "[#81a2be]检测到 API Key、模型或Baseurl缺失。请重新执行以下命令完成配置：[/#81a2be]\n"
        "[bold #b5bd68]novamind config[/bold #b5bd68]",
        title="[bold #81a2be] Boot Sequence Failed[/bold #81a2be]",
        border_style="#81a2be",
    ))


@app.command("run")
def run_agent(
    thread_id: str = typer.Option(None, "--thread-id", "-t", help="指定会话 ID（不传则自动创建新会话）"),
):
    """启动NovaMind智能体"""
    load_dotenv(ENV_PATH)
    provider = os.getenv("DEFAULT_PROVIDER")
    model = os.getenv("DEFAULT_MODEL")
    if not provider or not model:
        _show_boot_error()
        raise typer.Exit()
    if provider != "ollama":
        if provider in ["openai", "aliyun", "z.ai", "tencent", "other"]:
            if not os.getenv("OPENAI_API_KEY"):
                _show_boot_error()
                raise typer.Exit()
        elif provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                _show_boot_error()
                raise typer.Exit()

    import entry.main as novamind_main
    novamind_main.main(thread_id=thread_id)


@app.command("gui")
def run_gui_command(
    port: int = typer.Option(8765, "--port", "-p", help="后端监听端口"),
):
    """启动NovaMind桌面图形界面（pywebview 窗口）"""
    load_dotenv(ENV_PATH)
    provider = os.getenv("DEFAULT_PROVIDER")
    model = os.getenv("DEFAULT_MODEL")
    if not provider or not model:
        _show_boot_error()
        raise typer.Exit()
    if provider != "ollama":
        if provider in ["openai", "aliyun", "z.ai", "tencent", "other"]:
            if not os.getenv("OPENAI_API_KEY"):
                _show_boot_error()
                raise typer.Exit()
        elif provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                _show_boot_error()
                raise typer.Exit()

    try:
        import webview  # noqa: F401
    except ImportError:
        console.print(
            "[bold red]启动失败：缺少 pywebview 依赖！[/bold red]\n"
            "[dim]请运行 pip install pywebview 后重试。[/dim]"
        )
        raise typer.Exit(code=1) from None

    from novamind.webui.app import run_gui

    run_gui(port=port)


@app.command("monitor")
def run_monitor(
    thread_id: str = typer.Option(None, "--thread-id", "-t", help="指定监控的会话 ID"),
    list_sessions: bool = typer.Option(False, "--list", "-l", help="列出可监控的会话"),
):
    """启动NovaMind实时监控面板"""
    try:
        import entry.monitor as novamind_monitor
        novamind_monitor.main(thread_id=thread_id, list_sessions_mode=list_sessions)
    except ImportError as e:
        console.print(
            f"[bold red]启动失败：找不到监视器模块！[/bold red]\n"
            f"[dim]请确保 monitor.py 和 cli.py 在同一目录下。\n报错信息: {e}[/dim]"
        )


@app.command("doctor")
def run_doctor_command(
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出检查结果"),
):
    """检查 docs、policy、tool 契约和最近日志信号。"""
    report = run_doctor()
    counts = report.counts()

    if as_json:
        console.print_json(data=report.as_dict())
        if not report.ok:
            raise typer.Exit(code=1) from None
        return

    for finding in report.findings:
        if finding.level == "error":
            style = "bold #cc6666"
            label = "ERROR"
        elif finding.level == "warning":
            style = "bold #f0c674"
            label = "WARN"
        else:
            style = "bold #81a2be"
            label = "INFO"
        console.print(f"[{style}]{label}[/{style}] {finding.code}: {finding.message}")
        if finding.suggestion:
            console.print(f"[dim]  suggestion: {finding.suggestion}[/dim]")

    console.print()
    console.print(
        Panel(
            f"errors={counts['error']} warnings={counts['warning']} info={counts['info']}",
            title="NovaMind Doctor",
            border_style="#81a2be" if report.ok else "#f0c674",
        )
    )

    if not report.ok:
        raise typer.Exit(code=1) from None


def main():
    app()


if __name__ == "__main__":
    main()
