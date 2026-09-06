"""隔离压测实例启动器（真实模型压测 Phase 1/2，不进入普通 pytest）。

- 独立 workspace（NOVAMIND_WORKSPACE），不打生产数据库/日志/会话；
- 凭据从主 .env 显式读取后 set 到受控进程环境，不复制 .env 文件；
- 注入 MeteredChatModel（预算预留 + 调用级 usage 记录）到 WebRuntime，
  主回复与辅助调用（摘要/记忆/治理）共用同一实例；
- --precheck 模式不联网，验证构造、路由、预算拒绝与输出配置。

用法：
    python tests/performance/real_model_server.py --precheck \
        --workspace tests/performance/.run --budget 10 --calls-log tests/performance/.run/calls.jsonl
    python tests/performance/real_model_server.py \
        --workspace tests/performance/.run --budget 10 \
        --calls-log tests/performance/.run/calls.jsonl --port 8976
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 直接执行时（python file.py）保证项目根在 sys.path
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _resolve_main_env() -> Path:
    # tests/performance/real_model_server.py → 项目根
    return Path(__file__).resolve().parents[2] / ".env"


def build_metered_llm(provider: str, model: str, budget_yuan: float,
                      calls_log: str, api_key_env: str = "OPENAI_API_KEY"):
    """构造预算受控的真实模型包装。"""
    from novamind.core.provider import get_provider
    from tests.performance.harness import BudgetController
    from tests.performance.metered import MeteredChatModel

    inner = get_provider(provider_name=provider, model_name=model)
    budget = BudgetController(cap_yuan=budget_yuan, calls_log=calls_log)
    return MeteredChatModel(model=inner, budget=budget), budget


def precheck(provider: str, model: str, budget_yuan: float,
             calls_log: str, workspace: str) -> int:
    """不联网预检：验证模型构造、预算准入/拒绝、输出上限配置。"""
    from tests.performance.harness import BudgetController

    print(f"[precheck] provider={provider} model={model} budget={budget_yuan}")
    print(f"[precheck] workspace={workspace} calls_log={calls_log}")

    # 1) 模型构造（不 invoke）
    try:
        from novamind.core.provider import get_provider
        inner = get_provider(provider_name=provider, model_name=model)
    except Exception as exc:
        print(f"[precheck] FAIL 模型构造: {exc}")
        return 1
    print(f"[precheck] OK 模型构造 type={type(inner).__name__}")

    # 2) 计量包装构造
    from tests.performance.metered import MeteredChatModel
    budget = BudgetController(cap_yuan=budget_yuan, calls_log=calls_log)
    try:
        metered = MeteredChatModel(model=inner, budget=budget)
        metered.bind_tools([])
    except Exception as exc:
        print(f"[precheck] FAIL 计量包装/bind_tools: {exc}")
        return 1
    print(f"[precheck] OK 计量包装 bind_tools model_name={metered._model_name()}")

    # 3) 预算准入/拒绝（免费逻辑）
    if budget.try_reserve(est_input_tokens=100):
        print("[precheck] OK 预算准入（预留成功）")
    else:
        print("[precheck] FAIL 预算准入（应可预留）")
        return 1
    # 极端上限：cap 极小时应拒绝
    tiny = BudgetController(cap_yuan=0.0001, calls_log=None)
    if tiny.try_reserve(est_input_tokens=1000000000):
        print("[precheck] FAIL 预算拒绝（极小上限应拒绝）")
        return 1
    print("[precheck] OK 预算拒绝（极端上限被拒）")

    # 4) 环境隔离
    ws = os.environ.get("NOVAMIND_WORKSPACE", "")
    if ws and str(Path(workspace).resolve()) in ws:
        from novamind.core.config import DB_PATH
        if DB_PATH.startswith(ws):
            print(f"[precheck] OK workspace 隔离 DB_PATH={DB_PATH}")
        else:
            print(f"[precheck] WARN DB_PATH={DB_PATH} 不在独立 workspace 内")
    else:
        print("[precheck] FAIL NOVAMIND_WORKSPACE 未生效")
        return 1

    print("[precheck] PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NovaMind 隔离压测实例")
    ap.add_argument("--workspace", required=True, help="独立数据/工作目录")
    ap.add_argument("--budget", type=float, default=10.0, help="金额上限（元）")
    ap.add_argument("--calls-log", required=True, help="调用级 usage 输出 jsonl")
    ap.add_argument("--port", type=int, default=8976)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--precheck", action="store_true", help="不联网预检后退出")
    args = ap.parse_args()

    # 1) 环境：独立 workspace 必须在导入 novamind.core.config 之前设置
    ws = str(Path(args.workspace).resolve())
    os.environ["NOVAMIND_WORKSPACE"] = ws
    os.makedirs(ws, exist_ok=True)
    os.makedirs(Path(args.calls_log).resolve().parent, exist_ok=True)

    # 2) 凭据：从主 .env 显式读取（不复制文件）
    from dotenv import load_dotenv
    env_path = _resolve_main_env()
    if not env_path.exists():
        print(f"FATAL 找不到主 .env: {env_path}", file=sys.stderr)
        return 2
    load_dotenv(env_path, override=False)

    provider = args.provider or os.getenv("DEFAULT_PROVIDER", "other")
    model = args.model or os.getenv("DEFAULT_MODEL", "deepseek-v4-flash")

    if args.precheck:
        return precheck(provider, model, args.budget, args.calls_log, ws)

    # 3) 注入 metered llm 并启动（uvicorn 同进程，runtime factory 生效）
    metered, budget = build_metered_llm(
        provider, model, args.budget, args.calls_log)

    import novamind.webui.server as server
    from novamind.webui.runtime import WebRuntime

    server._runtime_factory = lambda: WebRuntime(llm=metered)

    print(f"[server] 启动隔离实例 127.0.0.1:{args.port} "
          f"provider={provider} model={model} budget=¥{args.budget}")
    import uvicorn
    uvicorn.run(server.app, host="127.0.0.1", port=args.port,
                log_level="warning")
    budget.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
