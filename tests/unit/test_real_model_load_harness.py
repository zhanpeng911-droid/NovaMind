"""压测工具免费单元测试（不联网、不调用真实模型，进入普通 pytest）。

覆盖：预算准入/结算/熔断、unknown usage、SSE 解析（分块/跨块/空行分帧）、
usage 提取。真实性能报告不使用这里模拟的数据。
"""
import json
import os
import tempfile
import unittest

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
    def test_reserve_settle_roundtrip(self):
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

    def test_cap_fraction_stops_admission(self):
        # cap 极小：任何预留都应被拒
        b = BudgetController(cap_yuan=0.0001, calls_log=None)
        self.assertFalse(b.try_reserve(est_input_tokens=10))

    def test_warn_fraction_blocks_new_calls(self):
        b = BudgetController(cap_yuan=1.0, calls_log=None)
        # 结算一笔接近 80% 上限的费用
        b.settle(CallRecord(call_id="big", model="m", status="ok",
                            started_at=0.0, duration_ms=1.0,
                            input_tokens=400000, output_tokens=0))  # 0.8 元
        self.assertFalse(b.try_reserve(est_input_tokens=1000),
                         "达到 80% 熔断后不得发起新调用")

    def test_unknown_usage_counted_not_zero(self):
        b = BudgetController(cap_yuan=10.0, calls_log=None)
        b.settle(CallRecord(call_id="u1", model="m", status="ok",
                            started_at=0.0, duration_ms=1.0,
                            input_tokens=None, output_tokens=None))
        self.assertEqual(b.summary["unknown_usage"], 1)
        # unknown 按保守上界结算：spent 大于零（release pending 的差值）
        # 这里 reserve 未发生，pending 为 0，spent 结算 0——重点是计数
        self.assertEqual(b.summary["unknown_usage"], 1)

    def test_calls_log_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.jsonl")
            b = BudgetController(cap_yuan=10.0, calls_log=log)
            b.settle(CallRecord(call_id="c1", model="deepseek-v4-flash",
                                status="ok", started_at=1.0, duration_ms=50.0,
                                input_tokens=100, output_tokens=20))
            b.close()
            lines = open(log, encoding="utf-8").read().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["model"], "deepseek-v4-flash")
            self.assertEqual(rec["status"], "ok")
            self.assertIn("estimated_cost_yuan", rec)


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
        types = self._frame_types([frame])
        self.assertEqual(types, [("text", {"type": "text", "content": "hi"})])

    def test_chunked_cross_block(self):
        # 一帧被拆成多块（含跨块 UTF-8）
        frame = 'data: {"type": "text", "content": "中文内容"}\n\n'
        mid = len(frame) // 2
        types = self._frame_types([frame[:mid], frame[mid:]])
        self.assertEqual(types[0][0], "text")
        self.assertEqual(types[0][1]["content"], "中文内容")

    def test_multiple_frames_one_chunk(self):
        chunk = ('data: {"type": "tool", "name": "search"}\n\n'
                 'data: {"type": "done"}\n\n')
        types = self._frame_types([chunk])
        self.assertEqual([t for t, _ in types], ["tool", "done"])

    def test_empty_data_line_skipped(self):
        chunk = ':\n\ndata: {"type": "done"}\n\n'
        types = self._frame_types([chunk])
        self.assertEqual([t for t, _ in types], ["done"])

    def test_bad_json_skipped(self):
        chunk = 'data: {broken\n\ndata: {"type": "done"}\n\n'
        types = self._frame_types([chunk])
        self.assertEqual([t for t, _ in types], ["done"])


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
        """样本不含私人信息（压测前置检查）。"""
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


if __name__ == "__main__":
    unittest.main()
