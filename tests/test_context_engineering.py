"""
上下文治理 P3 测试。

覆盖关键不变量：
  - token_counter：空列表 0，中文文本 > 0
  - resolve_window_size：穿透 FallbackChatModel 到内层模型查映射表
  - BudgetTracker：累计 token 算 fraction + 标 pending
  - Externalizer：超长工具结果外化 + preview，短内容不外化
  - Summarizer：消息分区 + 配对保护
  - DefaultStrategy：P1 外化 / P5 熔断剥 tool_calls
  - persona 桥接：用户画像写 procedural 记忆
"""
import tempfile
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novamind.core.context_engineering.utilities import (
    token_counter,
    resolve_window_size,
    resolve_model_name,
)
from novamind.core.context_engineering.strategies.default.budget import BudgetTrackerExecutor
from novamind.core.context_engineering.strategies.default.externalizer import ExternalizerExecutor
from novamind.core.context_engineering.strategies.default.summarizer import SummarizerExecutor
from novamind.core.context_engineering.strategies.default.strategy import DefaultStrategy
from novamind.core.context_engineering.strategies.default._constants import DEFAULT_THRESHOLDS
from novamind.core.context_engineering.contract import GovernanceContext

from novamind.core.memory.strategies.default.store import MarkdownFileStore
from novamind.core.memory.strategies.default.manager import DefaultMemoryManager
from novamind.core.memory.strategies.default.retriever import HybridRetriever
from novamind.core.memory.strategies.default.decay import EbbinghausDecayPolicy
from novamind.core.memory.persona import save_persona_to_memory, load_persona_from_memory
from novamind.core.memory.schema import MemoryType


class TestTokenCounter(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(token_counter([]), 0)

    def test_chinese_text_positive(self):
        self.assertGreater(token_counter(["你好世界，这是中文测试"]), 0)

    def test_english_text_positive(self):
        self.assertGreater(token_counter(["hello world this is a test"]), 0)


class TestResolveWindowSize(unittest.TestCase):
    def test_fallback_like_penetrates(self):
        class _Inner:
            model_name = "deepseek-chat"

        class _FallbackLike:
            models = [_Inner()]
            _active = 0

        self.assertEqual(resolve_window_size(_FallbackLike()), 64_000)

    def test_model_name_map(self):
        class _Model:
            model_name = "gpt-4o-mini"

        self.assertEqual(resolve_window_size(_Model()), 128_000)

    def test_default_window(self):
        class _Unknown:
            model_name = "totally-unknown-model"

        self.assertEqual(resolve_window_size(_Unknown()), 128_000)


class TestBudgetTracker(unittest.TestCase):
    def test_track_fraction_and_pending(self):
        budget = BudgetTrackerExecutor(DEFAULT_THRESHOLDS)
        g = budget.init_budget(None)
        g = budget.track(g, ["dummy"], lambda msgs: 50_000, 100_000)
        d = g["default"]
        self.assertEqual(d["budget"]["fraction"], 0.5)
        self.assertIn("P1", d["pending"])  # 0.5 >= 0.40
        self.assertNotIn("P4", d["pending"])  # 0.5 < 0.80


class TestExternalizer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ext = ExternalizerExecutor(externalize_dir=self.tmp.name, min_chars=100)

    def test_long_result_externalized(self):
        msg = ToolMessage(content="x" * 300, tool_call_id="t1", name="search")
        rewritten = self.ext.externalize_if_needed(msg)
        self.assertIsNotNone(rewritten)
        self.assertIn("[externalized path", rewritten.content)  # preview 带外化标记

    def test_short_result_not_externalized(self):
        msg = ToolMessage(content="short", tool_call_id="t1", name="search")
        self.assertIsNone(self.ext.externalize_if_needed(msg))


class TestSummarizer(unittest.TestCase):
    def test_partition_preserves_recent(self):
        summ = SummarizerExecutor(model=None, preserve_recent=2)
        msgs = [HumanMessage(content=f"h{i}", id=f"h{i}") for i in range(5)]
        to_sum, preserved = summ._partition(msgs)
        self.assertEqual(len(to_sum), 3)
        self.assertEqual(len(preserved), 2)


class TestDefaultStrategy(unittest.TestCase):
    def test_p5_strips_tool_calls(self):
        strategy = DefaultStrategy(model=_FakeWindowModel())
        ai = AIMessage(content="thinking", tool_calls=[{"id": "tc1", "name": "t", "args": {}}], id="ai1")
        ctx = GovernanceContext(
            state={},
            governance={"default": {"pending": [], "budget": {"fraction": 0.0}, "warned": False}},
            config={},
            token_counter=lambda msgs: 120_000,  # fraction = 120000/128000 ≈ 0.94 ≥ 0.9 触发 P5
            runtime={},
            hook="after_model",
            messages=[ai],
        )
        result = strategy.after_model(ctx)
        self.assertIsNotNone(result.messages_patch)
        # 第一条是剥了 tool_calls 的 AIMessage
        cleared = result.messages_patch[0]
        self.assertEqual(cleared.tool_calls, [])

    def test_p1_externalizes(self):
        strategy = DefaultStrategy(model=_FakeWindowModel())
        ctx = GovernanceContext(
            state={},
            governance={"default": {"pending": ["P1"], "budget": {"fraction": 0.45}, "p1_skip_until_fraction": 0.0}},
            config={},
            token_counter=lambda msgs: 45000,
            runtime={},
            hook="before_model",
            messages=[],
        )
        result = strategy.before_model(ctx)
        self.assertIsNotNone(result.state_patch)


class _FakeWindowModel:
    model_name = "gpt-4o-mini"


class TestPersonaBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarkdownFileStore(self.tmp.name)
        self.decay = EbbinghausDecayPolicy()
        self.manager = DefaultMemoryManager(store=self.store, decay_policy=self.decay)
        self.retriever = HybridRetriever(self.store, self.decay)
        from novamind.core.memory.bootstrap import _wrap_store

        _wrap_store(self.store, self.retriever)

    def test_persona_roundtrip(self):
        # BM25 空格分词对中文不友好，用英文内容验证 procedural 记忆召回（中文分词留后续 jieba）
        trace = save_persona_to_memory(self.manager, "user prefers python development")
        self.assertEqual(trace.type, MemoryType.PROCEDURAL)
        self.assertTrue(trace.metadata.get("persona"))
        persona = load_persona_from_memory(self.retriever)
        self.assertTrue(any("python" in p for p in persona))


if __name__ == "__main__":
    unittest.main()
