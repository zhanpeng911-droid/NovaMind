"""真实模型 SSE 压测客户端（真实模型压测 Phase 2/3，不进入普通 pytest）。

- httpx 异步流式消费 /chat SSE；闭环并发（虚拟用户结束当前轮次再发下一次）；
- 指标：HTTP 响应头时间、首个 text 帧时间（不标为 token TTFT）、轮次总耗时、
  终态（done 且无 error 帧才算成功）；主动取消与失败分表；
- 预算/上限停止条件：金额达到熔断、轮次/调用上限、连续 429/5xx；
- 默认第一轮只跑预热 3 + 基线 10（并发 1），确认费用后再扩大。

用法：
    python tests/performance/real_model_load.py \
        --base http://127.0.0.1:8976 --warmup 3 --baseline 10 --out tests/performance/.run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# 直接执行时（python file.py）保证项目根在 sys.path
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.performance.harness import SseParser  # noqa: E402


@dataclass
class TurnResult:
    thread_id: str
    ok: bool
    ended: bool              # 收到合法 done
    error_frame: str | None = None
    end_reason: str = ""
    status_code: int = 0
    connect_ms: float = 0.0
    first_text_ms: float = 0.0
    total_ms: float = 0.0
    text_len: int = 0
    tool_count: int = 0
    frame_types: list = field(default_factory=list)
    cancelled: bool = False


# 固定输入样本（不含私人信息）
WARMUP_SAMPLES = [
    "你好，用一句话介绍你自己。",
    "1+1 等于几？只回答数字。",
    "天空为什么是蓝色的？一句话。",
]
BASELINE_SAMPLES = [
    "用不超过 30 个字介绍大海。",
    "3+5 等于几？只回答数字。",
    "水的化学式是什么？只回答。",
    "什么是光合作用？一句话。",
    "地球到月球大约多远？一句话。",
    "用一句话解释什么是递归。",
    "中国最长的河流是哪条？只回答。",
    "维生素 C 的作用？一句话。",
    "2 的 10 次方是多少？只回答。",
    "用一句话说明什么是云计算。",
]


async def chat_once(client: httpx.AsyncClient, base: str,
                    message: str, thread_id: str,
                    timeout_s: float = 120.0) -> TurnResult:
    """一次 /chat 轮次。SSE 消费到 done；error 帧后 done 不判成功。"""
    parser = SseParser()
    r = TurnResult(thread_id=thread_id, ok=False, ended=False)
    t0 = time.monotonic()
    try:
        async with client.stream(
            "POST", f"{base}/chat",
            json={"message": message, "thread_id": thread_id},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        ) as resp:
            r.status_code = resp.status_code
            r.connect_ms = (time.monotonic() - t0) * 1000
            first_text = None
            async for chunk in resp.aiter_text():
                for frame in parser.feed(chunk):
                    r.frame_types.append(frame.type)
                    if frame.type == "text" and frame.data.get("content"):
                        r.text_len += len(frame.data.get("content", ""))
                        if first_text is None:
                            first_text = time.monotonic()
                    elif frame.type == "tool":
                        r.tool_count += 1
                    elif frame.type == "error":
                        r.error_frame = frame.data.get("message", "unknown")
                    elif frame.type == "done":
                        r.ended = True
            if first_text is not None:
                r.first_text_ms = (first_text - t0) * 1000
        r.total_ms = (time.monotonic() - t0) * 1000
        r.ok = r.ended and r.error_frame is None
        if not r.ended:
            r.end_reason = "no_done_frame"
        elif r.error_frame:
            r.end_reason = "error_frame"
    except httpx.TimeoutException:
        r.total_ms = (time.monotonic() - t0) * 1000
        r.end_reason = "timeout"
    except asyncio.CancelledError:
        r.total_ms = (time.monotonic() - t0) * 1000
        r.cancelled = True
        r.end_reason = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - 客户端采集任意失败
        r.total_ms = (time.monotonic() - t0) * 1000
        r.end_reason = f"client_error: {type(exc).__name__}"
    return r


async def run_turns(client, base, samples, tag, out, *, budget_ok) -> list[TurnResult]:
    """闭环并发跑一批轮次（并发=1 的预热/基线）。"""
    results: list[TurnResult] = []
    for i, msg in enumerate(samples):
        if not budget_ok():
            print(f"  [stop] 预算熔断，{tag} 在 {i} 轮停止")
            break
        tid = f"perf_{tag}_{i}_{int(time.time())}"
        r = await chat_once(client, base, msg, tid)
        results.append(r)
        status = "ok" if r.ok else f"FAIL({r.end_reason})"
        print(f"  [{tag} {i + 1}/{len(samples)}] {status} "
              f"{r.total_ms:.0f}ms first_text={r.first_text_ms:.0f}ms "
              f"text={r.text_len}B tools={r.tool_count}")
        if not r.ok and not r.cancelled:
            await asyncio.sleep(1)  # 失败退避，避免连续冲击
    return results


async def run_ladder(client, base, concurrency_list, per_tier, budget_ok,
                   tag="ladder"):
    """独立会话阶梯：每档开 N 个闭环 worker（虚拟用户结束当前轮次再发
    下一次），每 worker 独立 thread，共享该档 per_tier 轮次额度。"""
    results: list[TurnResult] = []
    samples = BASELINE_SAMPLES * 10  # 足够循环
    shared = {"done": 0}

    async def worker(wid: int):
        i = 0
        while shared["done"] < per_tier:
            if not budget_ok():
                print(f"  [stop] 预算熔断，{tag} c={shared['concurrency']} 在 "
                      f"{shared['done']} 轮停止")
                return
            msg = samples[i % len(samples)]
            tid = f"perf_{tag}_c{shared['concurrency']}_w{wid}_{i}_{int(time.time())}"
            r = await chat_once(client, base, msg, tid)
            results.append(r)
            shared["done"] += 1
            i += 1
            status = "ok" if r.ok else f"FAIL({r.end_reason})"
            print(f"  [{tag} c={shared['concurrency']} w{wid} "
                  f"{shared['done']}/{per_tier}] {status} "
                  f"{r.total_ms:.0f}ms first_text={r.first_text_ms:.0f}ms")
            if not r.ok:
                await asyncio.sleep(1)

    for c in concurrency_list:
        shared["concurrency"] = c
        shared["done"] = 0
        tasks = [asyncio.create_task(worker(w)) for w in range(c)]
        await asyncio.gather(*tasks)
    return results


def summarize(results: list[TurnResult]) -> dict:
    ok_times = [r.total_ms for r in results if r.ok]
    ft_times = [r.first_text_ms for r in results if r.ok and r.first_text_ms]
    def pct(vals, p):
        if not vals:
            return None
        return round(sorted(vals)[int(len(vals) * p) - 1], 1) if len(vals) else None
    return {
        "n": len(results),
        "ok": sum(1 for r in results if r.ok),
        "failed": [r.end_reason for r in results if not r.ok],
        "total_ms": {
            "p50": pct(ok_times, 0.5), "p95": pct(ok_times, 0.95),
            "max": round(max(ok_times), 1) if ok_times else None,
        },
        "first_text_ms": {
            "p50": pct(ft_times, 0.5), "p95": pct(ft_times, 0.95),
            "max": round(max(ft_times), 1) if ft_times else None,
        },
    }


async def main_async(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 预算停止条件由客户端维护（读取 calls.jsonl 累计花费）
    budget_cap = args.budget
    calls_log = out_dir / "calls.jsonl"

    def budget_ok() -> bool:
        if budget_cap is None:
            return True
        spent = 0.0
        if calls_log.exists():
            for line in calls_log.read_text(encoding="utf-8").splitlines():
                try:
                    spent += json.loads(line).get("estimated_cost_yuan", 0.0)
                except json.JSONDecodeError:
                    pass
        return spent < budget_cap * 0.8  # 80% 熔断

    results: dict[str, list[TurnResult]] = {}
    async with httpx.AsyncClient(base_url=args.base) as client:
        if args.scenario == "ladder":
            # 独立会话阶梯：并发 1→2→4，每档 per_tier 轮，每轮独立 thread
            for c in [int(x) for x in args.concurrency.split(",")]:
                print(f"== 独立会话阶梯 并发 {c}（每档 {args.per_tier} 轮） ==")
                results[f"ladder_c{c}"] = await run_ladder(
                    client, args.base, [c], args.per_tier, budget_ok,
                    tag=f"ladder_c{c}")
        elif args.scenario == "warmup":
            print("== 预热 (3) ==")
            results["warmup"] = await run_turns(
                client, args.base, WARMUP_SAMPLES, "warmup", out_dir,
                budget_ok=budget_ok)
            print("== 基线 (10) ==")
            results["baseline"] = await run_turns(
                client, args.base, BASELINE_SAMPLES, "baseline", out_dir,
                budget_ok=budget_ok)

    summary = {k: summarize(v) for k, v in results.items()}
    report = {"results": summary}
    report_path = out_dir / "summary.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print("\n== 摘要 ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告写入 {report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NovaMind 真实模型 SSE 压测客户端")
    ap.add_argument("--base", default="http://127.0.0.1:8976")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--baseline", type=int, default=10)
    ap.add_argument("--budget", type=float, default=10.0,
                    help="客户端侧预算熔断（元，可选）")
    ap.add_argument("--out", default="tests/performance/.run")
    ap.add_argument("--scenario", choices=["warmup", "ladder"], default="warmup")
    ap.add_argument("--concurrency", default="1,2,4",
                    help="ladder 场景并发档位，逗号分隔")
    ap.add_argument("--per-tier", type=int, default=20,
                    help="ladder 每档轮次上限")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
