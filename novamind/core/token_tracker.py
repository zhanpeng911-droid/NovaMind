"""
NovaMind Token追踪与成本估算模块

核心功能：
  1. 统计每次LLM调用的输入/输出Token数
  2. 基于模型定价自动估算单次调用和累计成本
  3. 提供成本预警（可配置阈值）
  4. 支持按会话维度聚合统计

定价数据来源：各厂商官网公开定价（2024年数据，实际使用时可能变动）
"""
from __future__ import annotations
import threading
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TokenUsage:
    """单次LLM调用的Token用量记录"""
    prompt_tokens: int = 0          # 输入Token数
    completion_tokens: int = 0      # 输出Token数
    total_tokens: int = 0           # 总Token数
    estimated_cost_usd: float = 0.0 # 预估成本（美元）
    model: str = ""                 # 使用的模型
    timestamp: str = ""             # 调用时间


@dataclass
class SessionStats:
    """单个会话的Token聚合统计"""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    call_count: int = 0             # LLM调用次数
    tool_call_count: int = 0        # 工具调用次数
    first_call_time: str = ""
    last_call_time: str = ""


# 主流模型的定价（美元/1K tokens）- 输入/输出分别定价
# 来源：各厂商官网 2024 年公开定价
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    # Anthropic
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-3-opus": (0.015, 0.075),
    # 通用回退定价（适用于未知模型）
    "_default": (0.001, 0.002),
}


class TokenTracker:
    """
    Token追踪器（线程安全）

    用法：
        tracker = TokenTracker(cost_warning_usd=1.0)
        tracker.record("gpt-4o-mini", prompt_tokens=500, completion_tokens=200)
        stats = tracker.get_session_stats("thread_123")
        print(f"累计花费: ${stats.total_cost_usd:.4f}")
    """

    def __init__(self, cost_warning_usd: float = 5.0):
        """
        Args:
            cost_warning_usd: 成本预警阈值（美元），超过此值会触发警告
        """
        self._lock = threading.Lock()
        self._usages: dict[str, list[TokenUsage]] = {}  # 按thread_id分组
        self._cost_warning = cost_warning_usd

    def record(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        thread_id: str = "default",
    ) -> TokenUsage:
        """
        记录一次LLM调用的Token用量

        Returns:
            TokenUsage 记录（包含预估成本）
        """
        total = prompt_tokens + completion_tokens

        # 计算预估成本
        input_rate, output_rate = MODEL_PRICING.get(
            model, MODEL_PRICING["_default"]
        )
        cost = (prompt_tokens * input_rate + completion_tokens * output_rate) / 1000

        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            estimated_cost_usd=cost,
            model=model,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        with self._lock:
            if thread_id not in self._usages:
                self._usages[thread_id] = []
            self._usages[thread_id].append(usage)

            # 成本预警
            stats = self._get_stats_unlocked(thread_id)
            if stats.total_cost_usd >= self._cost_warning:
                print(
                    f"\033[33m[NovaMind TokenTracker] ⚠️ 会话 {thread_id} "
                    f"累计成本已达到 ${stats.total_cost_usd:.4f}，"
                    f"超过预警阈值 ${self._cost_warning:.2f}\033[0m"
                )

        return usage

    def get_session_stats(self, thread_id: str = "default") -> SessionStats:
        """获取指定会话的聚合统计"""
        with self._lock:
            return self._get_stats_unlocked(thread_id)

    def _get_stats_unlocked(self, thread_id: str) -> SessionStats:
        """内部方法：获取统计（不加锁，由调用方保证线程安全）"""
        usages = self._usages.get(thread_id, [])
        if not usages:
            return SessionStats()

        stats = SessionStats(
            total_prompt_tokens=sum(u.prompt_tokens for u in usages),
            total_completion_tokens=sum(u.completion_tokens for u in usages),
            total_tokens=sum(u.total_tokens for u in usages),
            total_cost_usd=sum(u.estimated_cost_usd for u in usages),
            call_count=len(usages),
            first_call_time=usages[0].timestamp,
            last_call_time=usages[-1].timestamp,
        )
        return stats

    def get_all_thread_ids(self) -> list[str]:
        """获取所有已记录的会话ID"""
        with self._lock:
            return list(self._usages.keys())

    def format_stats(self, thread_id: str = "default") -> str:
        """格式化输出统计信息"""
        stats = self.get_session_stats(thread_id)
        return (
            f"会话 {thread_id} Token统计:\n"
            f"  输入Token: {stats.total_prompt_tokens:,}\n"
            f"  输出Token: {stats.total_completion_tokens:,}\n"
            f"  总Token: {stats.total_tokens:,}\n"
            f"  预估成本: ${stats.total_cost_usd:.4f}\n"
            f"  LLM调用次数: {stats.call_count}\n"
            f"  统计时间: {stats.first_call_time} ~ {stats.last_call_time}"
        )

    def reset(self, thread_id: str | None = None):
        """重置统计（清空指定会话或全部）"""
        with self._lock:
            if thread_id:
                self._usages.pop(thread_id, None)
            else:
                self._usages.clear()


# 全局Token追踪器实例
token_tracker = TokenTracker()
