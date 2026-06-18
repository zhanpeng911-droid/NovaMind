"""
NovaMind Token追踪器基准测试

演示Token追踪和成本估算功能。
"""
from novamind.core.token_tracker import TokenTracker


def main():
    tracker = TokenTracker(cost_warning_usd=1.0)

    # 模拟多次LLM调用
    calls = [
        ("gpt-4o", 500, 200),
        ("gpt-4o-mini", 1000, 500),
        ("claude-3-5-sonnet", 300, 800),
        ("gpt-4o", 2000, 1000),
        ("gpt-4o-mini", 500, 300),
    ]

    print("=== NovaMind Token追踪器基准测试 ===\n")

    for model, prompt, completion in calls:
        usage = tracker.record(model, prompt, completion, thread_id="benchmark")
        print(
            f"模型: {model:20s} | "
            f"输入: {usage.prompt_tokens:>6,} | "
            f"输出: {usage.completion_tokens:>6,} | "
            f"成本: ${usage.estimated_cost_usd:.6f}"
        )

    print(f"\n{'='*60}")
    print(tracker.format_stats("benchmark"))


if __name__ == "__main__":
    main()
