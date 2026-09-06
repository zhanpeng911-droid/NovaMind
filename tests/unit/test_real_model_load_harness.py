"""压测工具免费单元测试（不联网、不调用真实模型，进入普通 pytest）。

覆盖（个人收尾 Phase 1/2）：预算准入/结算/熔断、unknown 不清零（保留预留
风险金额）、部分 usage、异常/取消记录、无效预算拒绝、非空日志拒绝复用、
模型构造收到输出/超时/重试参数、run_ladder 恰好 per_tier、SSE 解析与
usage 提取。真实性能报告不使用这里模拟的数据。
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.performance.harness import (
    BudgetController,
    CallRecord,
    SseParser,
    extract_usage,
)
from tests.performance.real_model_load import (
    BASELINE_SAMPLES,
    WARMUP_SAMPLES,
    summarize,
)


class TestBudgetController(unittest.TestCase):
    def test_reserve_settle_roundtrip_releases_once(self):
        b = BudgetController(cap_yuan=10.0, calls_log=None)
        reserve = b.try_reserve(est_input_tokens=1000)
        self.assertIsNotNone(reserve)
        b.settle(CallRecord(call_id="c1", model="m", status="ok",
                            started_at=0.0, duration_ms=100.0,
                            input_tokens=1000, output_tokens=200),
                 reserve=reserve)
        s = b.summary
        self.assertGreater(s["spent_yuan"], 0)
        self.assertAlmostEqual(s["pending_yuan"], 0.0, places=9)
        self.assertEqual(s["unknown_usage"], 0)

    def test_unknown_usage_keeps_reserve_risk(self):
        """unknown（usage 缺失）：预留保留为风险金额，不按免费清零。"""
        b = BudgetController(cap_yuan=10.0, calls_log=None)
        reserve = b.try_reserve(est_input_tokens=1000)
        self.assertIsNotNone(reserve)
        b.settle(CallRecord(call_id="u1", model="m", status="ok",
                            started_at=0.0, duration_ms=1.0,
                            input_tokens=None, output_tokens=None),
                 reserve=reserve)
        s = b.summary
        self.assertEqual(s["unknown_usage"], 1)
        self.assertGreater(s["unknown_reserved_yuan"], 0,
                           "unknown 的预留必须保留为风险金额")
        self.assertGreater(s["pending_yuan"], 0,
                           "unknown 不释放预留（不按免费处理）")

    def test_partial_usage_also_unknown(self):
        """部分 usage（只有 output）：同样按 unknown 保留风险金额。"""
        b = BudgetController(cap_yuan=10.0, calls_log=None)
        reserve = b.try_reserve(est_input_tokens=500)
        b.settle(CallRecord(call_id="p1", model="m", status="ok",
                            started_at=0.0, duration_ms=1.0,
                            input_tokens=None, output_tokens=50),
                 reserve=reserve)
        self.assertEqual(b.summary["unknown_usage"], 1)
        self.assertGreater(b.summary["unknown_reserved_yuan"], 0)

    def test_error_settle_releases_reserve(self):
        """异常调用（status=error，无 usage）：预留保留为风险。"""
        b = BudgetController(cap_yuan=10.0, calls_log=None)
        reserve = b.try_reserve(est_input_tokens=500)
        b.settle(CallRecord(call_id="e1", model="m", status="error",
                            started_at=0.0, duration_ms=1.0,
                            input_tokens=None, output_tokens=None,
                            error="boom"),
                 reserve=reserve)
        self.assertEqual(b.summary["unknown_usage"], 1)
        self.assertGreater(b.summary["pending_yuan"], 0)

    def test_actual_cost_exceeding_reserve_kept_truthful(self):
        """实际 usage 超预留：如实记账（spent 不截断），后续准入被拒。"""
        b = BudgetController(cap_yuan=1.0, calls_log=None)
        reserve = b.try_reserve(est_input_tokens=100)  # 小预留
        b.settle(CallRecord(call_id="big", model="m", status="ok",
                            started_at=0.0, duration_ms=1.0,
                            input_tokens=400000, output_tokens=50000),  # 1.2 元
                 reserve=reserve)
        self.assertGreater(b.summary["spent_yuan"], 1.0,
                           "实际费用超预留必须如实记账，不截断到预算内")
        self.assertFalse(b.try_reserve(est_input_tokens=10),
                         "超支后不得发起新调用")

    def test_invalid_budget_or_price_rejected(self):
        for bad in (float("nan"), float("inf"), -1.0, 0.0, "abc"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                BudgetController(cap_yuan=bad, calls_log=None)
        with self.assertRaises(ValueError):
            BudgetController(cap_yuan=10.0, price={"input": float("nan"),
                                                   "output": 8.0},
                             calls_log=None)
        with self.assertRaises(ValueError):
            BudgetController(cap_yuan=10.0, price={"input": -1.0,
                                                   "output": 8.0},
                             calls_log=None)

    def test_calls_log_written_with_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.jsonl")
            b = BudgetController(cap_yuan=10.0, calls_log=log)
            reserve = b.try_reserve(est_input_tokens=100)
            b.settle(CallRecord(call_id="c1", model="deepseek-v4-flash",
                                status="ok", started_at=1.0, duration_ms=50.0,
                                input_tokens=100, output_tokens=20),
                     reserve=reserve)
            b.close()
            lines = open(log, encoding="utf-8").read().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["model"], "deepseek-v4-flash")
            self.assertIn("estimated_cost_yuan", rec)
            self.assertIn("reserved_yuan", rec)


class TestServerGuards(unittest.TestCase):
    def test_nonempty_calls_log_rejects_restart(self):
        from tests.performance.real_model_server import _calls_log_guard
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.jsonl")
            # 空文件：允许（新批次）
            open(log, "w", encoding="utf-8").close()
            self.assertIsNone(_calls_log_guard(log, 10.0))
            # 非空：拒绝复用
            with open(log, "a", encoding="utf-8") as f:
                f.write('{"call_id":"x"}\n')
            err = _calls_log_guard(log, 10.0)
            self.assertIsNotNone(err)
            self.assertIn("已非空", err)

    def test_missing_budget_rejected(self):
        from tests.performance.real_model_server import _calls_log_guard
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.jsonl")
            err = _calls_log_guard(log, None)
            self.assertIsNotNone(err)
            self.assertIn("--budget 必填", err)

    def test_build_metered_llm_passes_output_limit(self):
        """模型构造必须实际收到 max_tokens/timeout/max_retries。"""
        from tests.performance.real_model_server import build_metered_llm
        captured = {}

        def fake_get_provider(*, provider_name, model_name, **kwargs):
            captured.update(kwargs)
            return object()

        with patch("novamind.core.provider.get_provider", fake_get_provider):
            with tempfile.TemporaryDirectory() as tmp:
                llm, budget = build_metered_llm(
                    "other", "deepseek-v4-flash", 5.0,
                    os.path.join(tmp, "calls.jsonl"),
                    max_output_tokens=2000, timeout_s=60.0, retries=0)
                budget.close()
        self.assertEqual(captured.get("max_tokens"), 2000)
        self.assertEqual(captured.get("timeout"), 60.0)
        self.assertEqual(captured.get("max_retries"), 0)


class TestLadderDispatch(unittest.TestCase):
    def test_ladder_exactly_per_tier_no_over_dispatch(self):
        """并发 1/2/4 每档 20：修复超发后恰好 20（多 worker 不重复领取）。"""
        from tests.performance import real_model_load as load_mod

        async def fake_chat_once(client, base, message, thread_id,
                                 timeout_s=120.0):
            from tests.performance.real_model_load import TurnResult
            return TurnResult(thread_id=thread_id, ok=True, ended=True,
                              total_ms=10.0, first_text_ms=5.0)

        original = load_mod.chat_once
        load_mod.chat_once = fake_chat_once
        try:
            async def _run():
                results = []
                for c in (1, 2, 4):
                    r = await load_mod.run_ladder(
                        object(), "http://x", [c], 20,
                        lambda: True, tag=f"c{c}")
                    results.append((c, r))
                return results

            tiers = asyncio.run(_run())
        finally:
            load_mod.chat_once = original

        for c, r in tiers:
            self.assertEqual(len(r), 20, f"并发 {c} 档应恰好 20 轮")
            # 整档样本 idx 覆盖 0..19 无重复（同一份固定样本集合）
            idxs = sorted(int(t.thread_id.split("_")[-2]) for t in r)
            self.assertEqual(idxs, list(range(20)), f"并发 {c} 档样本序一致")

    def test_ladder_zero_budget_ok_skips(self):
        from tests.performance import real_model_load as load_mod

        async def fake_chat_once(client, base, message, thread_id,
                                 timeout_s=120.0):
            from tests.performance.real_model_load import TurnResult
            return TurnResult(thread_id=thread_id, ok=True, ended=True)

        original = load_mod.chat_once
        load_mod.chat_once = fake_chat_once
        try:
            async def _run():
                return await load_mod.run_ladder(
                    object(), "http://x", [1], 20, lambda: False,
                    tag="no_budget")
            r = asyncio.run(_run())
        finally:
            load_mod.chat_once = original
        self.assertEqual(r, [], "预算不可用时不得发起任何轮次")


class TestSseParser(unittest.TestCase):
    def _frame_types(self, chunks):
        p = SseParser()
        types = []
        for c in chunks:
            for f in p.feed(c):
                types.append((f.type, f.data))
        return types

    def test_framing(self):
        frame = 'data: {"type": "text", "content": "hi"}\n\n'
        self.assertEqual(self._frame_types([frame]),
                         [("text", {"type": "text", "content": "hi"})])

    def test_chunked_cross_block(self):
        frame = 'data: {"type": "text", "content": "中文内容"}\n\n'
        mid = len(frame) // 2
        types = self._frame_types([frame[:mid], frame[mid:]])
        self.assertEqual(types[0][0], "text")
        self.assertEqual(types[0][1]["content"], "中文内容")

    def test_multiple_frames_one_chunk(self):
        chunk = ('data: {"type": "tool", "name": "search"}\n\n'
                 'data: {"type": "done"}\n\n')
        self.assertEqual([t for t, _ in self._frame_types([chunk])],
                         ["tool", "done"])

    def test_bad_json_counted_not_silent(self):
        """坏 JSON 帧必须计数（不得静默跳过后算全成功）。"""
        p = SseParser()
        p.feed('data: {broken\n\ndata: {"type": "done"}\n\n')
        self.assertEqual(p.bad_frames, 1)


class TestUsageExtract(unittest.TestCase):
    class FakeResp:
        def __init__(self, usage=None, meta=None):
            self.usage_metadata = usage
            self.response_metadata = meta

    def test_usage_metadata(self):
        r = self.FakeResp(usage={"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(extract_usage(r), (10, 5))

    def test_response_metadata_token_usage(self):
        r = self.FakeResp(meta={"token_usage": {"prompt_tokens": 7,
                                                "completion_tokens": 3}})
        self.assertEqual(extract_usage(r), (7, 3))

    def test_missing_usage(self):
        self.assertEqual(extract_usage(self.FakeResp()), (None, None))


class TestSummarize(unittest.TestCase):
    def test_baseline_samples_are_private_free(self):
        all_text = " ".join(WARMUP_SAMPLES + BASELINE_SAMPLES).lower()
        for banned in ("password", "key=", "token=", "api_key"):
            self.assertNotIn(banned, all_text)

    def test_summarize_ok(self):
        from tests.performance.real_model_load import TurnResult
        results = [TurnResult(thread_id=f"t{i}", ok=True, ended=True,
                              total_ms=100.0 + i, first_text_ms=50.0)
                   for i in range(10)]
        s = summarize(results)
        self.assertEqual(s["n"], 10)
        self.assertEqual(s["ok"], 10)
        self.assertIsNotNone(s["total_ms"]["p50"])
        self.assertEqual(s["failed"], [])

    def test_percentile_nearest_rank(self):
        from tests.performance.real_model_load import summarize, TurnResult
        # 100 个样本：p95 取 ceil(100*0.95)-1 = 94 → sorted[94]
        results = [TurnResult(thread_id=f"t{i}", ok=True, ended=True,
                              total_ms=float(i), first_text_ms=0.0)
                   for i in range(100)]
        s = summarize(results)
        self.assertEqual(s["total_ms"]["p95"], 94.0)
        self.assertEqual(s["total_ms"]["p50"], 49.0)


if __name__ == "__main__":
    unittest.main()
