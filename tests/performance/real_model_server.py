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
                      calls_log: str, *,
                      max_output_tokens: int | None = None,
                      timeout_s: float = 60.0, retries: int = 0,
                      price: dict | None = None,
                      api_key_env: str = "OPENAI_API_KEY"):
    """构造预算受控的真实模型包装。

    - 输出上限/超时/重试显式传给真实模型（与预留上限共用同一配置来源）；
    - 单价/预算由 BudgetController 校验有限有效数；
    - 模型不支持传入参数时构造即失败（不会静默忽略）。
    """
    from novamind.core.provider import get_provider
    from tests.performance.harness import (
        BudgetController,
        MAX_OUTPUT_TOKENS,
    )
    from tests.performance.metered import MeteredChatModel

    max_tokens = max_output_tokens or MAX_OUTPUT_TOKENS
    inner = get_provider(
        provider_name=provider, model_name=model,
        max_tokens=max_tokens, timeout=timeout_s, max_retries=retries,
    )
    budget = BudgetController(cap_yuan=budget_yuan, price=price,
                              calls_log=calls_log)
    return MeteredChatModel(model=inner, budget=budget), budget


def precheck(provider: str, model: str, budget_yuan: float,
             calls_log: str, workspace: str,
             max_output_tokens: int = 2000,
             timeout_s: float = 60.0, retries: int = 0) -> int:
    """不联网预检：验证模型构造、输出上限/超时/重试实际送达、
    预算准入/拒绝、环境隔离。不写入付费调用日志。"""
    from tests.performance.harness import BudgetController

    print(f"[precheck] provider={provider} model={model} budget={budget_yuan}")
    print(f"[precheck] workspace={workspace} calls_log={calls_log}")
    print(f"[precheck] 模型设置 max_tokens={max_output_tokens} "
          f"timeout={timeout_s}s retries={retries}")

    # 1) 模型构造并验证输出上限/超时/重试实际送达（不 invoke）
    try:
        from novamind.core.provider import get_provider
        inner = get_provider(
            provider_name=provider, model_name=model,
            max_tokens=max_output_tokens, timeout=timeout_s,
            max_retries=retries,
        )
    except Exception as exc:
        print(f"[precheck] FAIL 模型构造（含输出上限参数）: {exc}")
        return 1
    got_max = getattr(inner, "max_tokens", None)
    got_timeout = getattr(inner, "timeout", None)
    got_retries = getattr(inner, "max_retries", None)
    if got_max != max_output_tokens:
        print(f"[precheck] FAIL 输出上限未送达：模型 max_tokens={got_max!r}")
        return 1
    print(f"[precheck] OK 模型构造 type={type(inner).__name__} "
          f"max_tokens={got_max} timeout={got_timeout} retries={got_retries}")

    # 2) 计量包装构造
    from tests.performance.metered import MeteredChatModel
    budget = BudgetController(cap_yuan=budget_yuan, calls_log=None)
    try:
        metered = MeteredChatModel(model=inner, budget=budget)
        metered.bind_tools([])
    except Exception as exc:
        print(f"[precheck] FAIL 计量包装/bind_tools: {exc}")
        return 1
    print(f"[precheck] OK 计量包装 bind_tools model_name={metered._model_name()}")

    # 3) 预算准入/拒绝（免费逻辑，calls_log=None 不写日志）
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
    # 无效预算/价格必须拒绝
    for bad in (float("nan"), float("inf"), -1.0, 0.0):
        try:
            BudgetController(cap_yuan=bad, calls_log=None)
            print(f"[precheck] FAIL 无效预算 {bad!r} 未被拒绝")
            return 1
        except ValueError:
            pass
    print("[precheck] OK 无效预算/价格被拒（NaN/inf/非正）")

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


def _calls_log_guard(calls_log: str, budget: float | None) -> str | None:
    """重启保护检查。返回错误信息（None 表示通过）。

    - budget 未显式填写 → 拒绝（不依赖默认金额）；
    - calls.jsonl 已非空 → 拒绝复用（防误操作恢复成全额预算）。"""
    if budget is None:
        return "--budget 必填：每次新批次必须显式填写剩余额度，不依赖默认值"
    p = Path(calls_log)
    if p.exists() and p.stat().st_size > 0:
        return (f"calls.jsonl 已非空：{p}。请先核对供应商用量与旧记录中的"
                "已花费用，再指定新的批次目录与剩余额度；"
                "不要删除旧记录来绕过检查。")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="NovaMind 隔离压测实例")
    ap.add_argument("--workspace", required=True, help="独立数据/工作目录")
    ap.add_argument("--budget", type=float, default=None,
                    help="单批次金额上限（元，必填；不依赖默认值）")
    ap.add_argument("--calls-log", required=True, help="调用级 usage 输出 jsonl")
    ap.add_argument("--port", type=int, default=8976)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-output-tokens", type=int, default=2000)
    ap.add_argument("--timeout-s", type=float, default=60.0)
    ap.add_argument("--retries", type=int, default=0)
    ap.add_argument("--precheck", action="store_true", help="不联网预检后退出")
    args = ap.parse_args()

    # 1) 环境：独立 workspace 必须在导入 novamind.core.config 之前设置
    ws = str(Path(args.workspace).resolve())
    os.environ["NOVAMIND_WORKSPACE"] = ws
    os.makedirs(ws, exist_ok=True)
    calls_log_path = Path(args.calls_log).resolve()
    os.makedirs(calls_log_path.parent, exist_ok=True)

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
        # 预检不写付费调用日志（calls_log 内部传 None），不污染真实批次
        return precheck(provider, model, args.budget or 10.0,
                        str(calls_log_path), ws,
                        max_output_tokens=args.max_output_tokens,
                        timeout_s=args.timeout_s, retries=args.retries)

    # 3) 重启保护：calls.jsonl 已非空或 budget 未填 → 拒绝启动
    guard_error = _calls_log_guard(str(calls_log_path), args.budget)
    if guard_error is not None:
        print(f"FATAL {guard_error}", file=sys.stderr)
        return 2

    # 4) 注入 metered llm 并启动（uvicorn 同进程，runtime factory 生效）
    metered, budget = build_metered_llm(
        provider, model, args.budget, str(calls_log_path),
        max_output_tokens=args.max_output_tokens,
        timeout_s=args.timeout_s, retries=args.retries)

    import novamind.webui.server as server
    from novamind.webui.runtime import WebRuntime

    server._runtime_factory = lambda: WebRuntime(llm=metered)

    print(f"[server] 启动隔离实例 127.0.0.1:{args.port} "
          f"provider={provider} model={model} budget=¥{args.budget} "
          f"max_tokens={args.max_output_tokens} timeout={args.timeout_s}s")
    import uvicorn
    uvicorn.run(server.app, host="127.0.0.1", port=args.port,
                log_level="warning")
    budget.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
