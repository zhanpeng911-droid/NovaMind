"""真实模型压测：预算控制、调用记录与 SSE 解析（不进入普通 pytest）。

- BudgetController：调用前保守预留（输入 + 最大输出），调用后按真实
  usage 结算；达到上限 80% 停止发起新调用，余额不足一次调用时直接拒绝。
  估算单价非供应商结算，报告中明确标注。
- CallRecorder：调用级 usage 结构化写入 calls.jsonl（model/usage/耗时/
  request_id/status），不经过会遮盖 token 字段的审计通道。
- parse_sse：SSE 分块/跨块 UTF-8/空行分帧解析，返回帧类型与 payload。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass


# 保守估算单价（元 / 百万 token）——参考 deepseek-chat 官方价，非结算价
DEFAULT_PRICE = {"input": 2.0, "output": 8.0, "cache_hit_input": 0.5}
# 单次调用预留用的最大输出 token（保守上界）
MAX_OUTPUT_TOKENS = 2000
# 预算达到该比例停止发起新调用（等在途收尾）
WARN_FRACTION = 0.8


class BudgetExceededError(RuntimeError):
    """预算不足或达到熔断比例，拒绝发起新调用。"""


@dataclass
class CallRecord:
    call_id: str
    model: str
    status: str            # ok / error / cancelled / unknown
    started_at: float
    duration_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float = 0.0
    reserved_yuan: float = 0.0   # 该笔调用预留金额（unknown 时保留为风险）
    request_id: str | None = None
    error: str | None = None


def _is_finite_positive(value: float) -> bool:
    """有限且为正的数值（拒绝 NaN/±inf/≤0）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return f > 0 and f == f and f not in (float("inf"), float("-inf"))


class BudgetController:
    """按金额上限做准入控制。

    预留 = 输入 token 估算 + 最大输出预留；结算按真实 usage 回写。
    unknown usage（无 usage_metadata）按保守上界结算并单独计数。
    线程安全：模型调用运行在线程池，可并发结算。
    """

    def __init__(self, cap_yuan: float, *, price: dict | None = None,
                 calls_log: str | None = None):
        # 金额/单价必须是有限有效数：拒绝 NaN/无穷/非正预算与无效价格
        if not _is_finite_positive(cap_yuan):
            raise ValueError(f"预算必须为正有限数，收到 {cap_yuan!r}")
        if price is None:
            price = dict(DEFAULT_PRICE)
        for key, val in price.items():
            if not _is_finite_positive(val):
                raise ValueError(f"单价 {key}={val!r} 无效（须为正有限数）")
        self.cap = float(cap_yuan)
        self.price = dict(price)
        self.spent = 0.0           # 已结算
        self.pending = 0.0         # 预留中：在途 + 已结束但费用未知
        self.unknown_usage = 0     # 无法计量次数
        self.unknown_reserved = 0.0  # 已结束但费用未知的保留风险金额
        self.calls_log = calls_log
        self._lock = threading.Lock()
        self._log_handle = None
        if calls_log:
            os.makedirs(os.path.dirname(calls_log) or ".", exist_ok=True)
            self._log_handle = open(calls_log, "a", encoding="utf-8")

    def _est_input_cost(self, est_input_tokens: int) -> float:
        return est_input_tokens / 1e6 * self.price["input"]

    def try_reserve(self, est_input_tokens: int = 1000) -> float | None:
        """调用前预留费用。返回预留金额（float）；预算不足返回 None
        （调用方应记 cancelled 并停止发起新轮次）。"""
        reserve = (self._est_input_cost(max(0, est_input_tokens))
                   + MAX_OUTPUT_TOKENS / 1e6 * self.price["output"])
        with self._lock:
            total = self.spent + self.pending
            if total >= self.cap * WARN_FRACTION:
                return None
            if self.cap - total < reserve:
                return None
            self.pending += reserve
            return reserve

    def settle(self, record: CallRecord, reserve: float = 0.0) -> float:
        """调用结束结算。

        完整 usage（输入+输出均有效）：按真实费用记账，释放该笔预留
        （只释放一次）；实际费用超过预留时如实记账、不截断到预算内
        （后续准入因 spent 已高而被拒绝）。

        usage 缺失/部分缺失/无法确认：**不按免费处理**——保留该笔预留
        作为风险金额（pending 不释放），计入 unknown 并记录预留值；
        报告明确 pending 包含“仍在途”与“已结束但费用未知”。"""
        complete = (record.input_tokens is not None
                    and record.output_tokens is not None)
        cost = (estimate_cost(record.input_tokens, record.output_tokens,
                              self.price) if complete else 0.0)
        with self._lock:
            if not complete:
                self.unknown_usage += 1
                self.unknown_reserved += reserve
                # pending 保留该笔预留（不释放），后续准入将其计入占用
            else:
                self.pending = max(0.0, self.pending - reserve)
                self.spent += cost
        record.estimated_cost = cost  # 回写供 calls.jsonl 记录
        self._log(record)
        return cost

    def _log(self, record: CallRecord) -> None:
        if self._log_handle is None:
            return
        line = {
            "call_id": record.call_id,
            "model": record.model,
            "status": record.status,
            "started_at": record.started_at,
            "duration_ms": round(record.duration_ms, 3),
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "estimated_cost_yuan": round(record.estimated_cost, 6),
            "reserved_yuan": round(record.reserved_yuan, 6),
            "request_id": record.request_id,
            "error": record.error,
        }
        try:
            self._log_handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._log_handle.flush()
        except OSError:
            pass

    def close(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    @property
    def summary(self) -> dict:
        with self._lock:
            return {
                "cap_yuan": self.cap,
                "spent_yuan": round(self.spent, 6),
                "pending_yuan": round(self.pending, 6),
                "unknown_usage": self.unknown_usage,
                "unknown_reserved_yuan": round(self.unknown_reserved, 6),
            }


def estimate_cost(input_tokens: int | None, output_tokens: int | None,
                  price: dict | None = None) -> float:
    """按真实 usage 估算费用（估算价，非结算价）。"""
    if input_tokens is None or output_tokens is None:
        return 0.0
    p = dict(price or DEFAULT_PRICE)
    return (input_tokens / 1e6 * p["input"]
            + output_tokens / 1e6 * p["output"])


def extract_usage(message: object) -> tuple[int | None, int | None]:
    """从 LLM 响应提取 (input_tokens, output_tokens)。兼容
    usage_metadata 与 response_metadata.token_usage。"""
    usage = getattr(message, "usage_metadata", None)
    meta = getattr(message, "response_metadata", None) or {}
    if isinstance(usage, dict):
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        if inp is not None or out is not None:
            return inp, out
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(token_usage, dict):
        inp = token_usage.get("prompt_tokens") or token_usage.get("input_tokens")
        out = token_usage.get("completion_tokens") or token_usage.get("output_tokens")
        if inp is not None or out is not None:
            return inp, out
    return None, None


# ── SSE 解析 ─────────────────────────────────────────────────────────

class SseFrame:
    __slots__ = ("type", "data")

    def __init__(self, type_: str, data: dict):
        self.type = type_
        self.data = data


class SseParser:
    """增量 SSE 解析器：不假定一次网络读取对应一个 JSON，
    支持分块、跨块 UTF-8、空行分帧。"""

    def __init__(self):
        self._buf = ""
        self.bad_frames = 0   # 无法解析的 data 帧计数（坏 JSON）

    def feed(self, chunk: str) -> list[SseFrame]:
        self._buf += chunk
        frames: list[SseFrame] = []
        while True:
            idx = self._buf.find("\n\n")
            if idx == -1:
                break
            block = self._buf[:idx]
            self._buf = self._buf[idx + 2:]
            data_lines = [ln[6:].strip() for ln in block.split("\n")
                          if ln.startswith("data: ")]
            if not data_lines:
                continue
            try:
                payload = json.loads("".join(data_lines))
            except (json.JSONDecodeError, ValueError):
                self.bad_frames += 1  # 坏帧不静默跳过后算全成功
                continue
            if isinstance(payload, dict):
                frames.append(SseFrame(payload.get("type", ""), payload))
        return frames


def new_call_id() -> str:
    return uuid.uuid4().hex
