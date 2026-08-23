"""
五层记忆 P2 测试。

覆盖关键不变量：
  - Schema：MemoryTrace frozen，with_strength/with_operation 创建新实例，日志上限 20
  - Decay：lazy 计算不修改 trace，值钳制 [0,1]，episodic 衰减快于 semantic
  - Decay 数学不变量：精确公式 / 时间单调 / 类型排序 / access boost / config 覆盖隔离
  - Forget：TTL 过期 / strength 阈值两规则
  - Store：MarkdownFileStore 增删改查 + 序列化 roundtrip
  - Retriever：BM25 命中 + 强化写回 + forgotten 过滤
  - Manager：encode 幂等 / associate 双向 / consolidate 合并+标记 forgotten / reconsolidate 保留强度
"""
import math
import tempfile
import time
import unittest
from dataclasses import replace

from novamind.core.memory.schema import MemoryTrace, MemoryType, OperationLog
from novamind.core.memory.config import get_memory_config, set_memory_config
from novamind.core.memory.strategies.default.decay import EbbinghausDecayPolicy
from novamind.core.memory.strategies.default.forget import CompositeForgetPolicy
from novamind.core.memory.strategies.default.store import MarkdownFileStore
from novamind.core.memory.strategies.default.retriever import HybridRetriever
from novamind.core.memory.strategies.default.manager import DefaultMemoryManager
from novamind.core.memory.strategies.default.strategy import build_default_provider
from novamind.core.memory.strategies.default._constants import (
    DECAY_COEFFICIENTS,
    DECAY_PARAMS,
)
from novamind.core.memory.types import MemoryQuery


class TestSchema(unittest.TestCase):
    def test_frozen_with_strength_creates_new_instance(self):
        t = MemoryTrace(id="1", content="c", type=MemoryType.EPISODIC)
        t2 = t.with_strength(0.5, time.time())
        self.assertIsNot(t, t2)
        self.assertEqual(t2.strength, 0.5)
        self.assertEqual(t2.access_count, 1)

    def test_operation_log_capped_at_20(self):
        t = MemoryTrace(id="1", content="c", type=MemoryType.EPISODIC)
        for i in range(25):
            t = t.with_operation(OperationLog(timestamp=i, operation="encode"))
        self.assertEqual(len(t.operation_log), 20)


class TestDecay(unittest.TestCase):
    def setUp(self):
        self.policy = EbbinghausDecayPolicy()

    def test_compute_does_not_modify_trace(self):
        t = MemoryTrace(id="1", content="c", type=MemoryType.EPISODIC, created_at=time.time())
        before = (t.strength, t.access_count)
        self.policy.compute_strength(t, time.time())
        self.assertEqual((t.strength, t.access_count), before)

    def test_strength_clamped(self):
        t = MemoryTrace(id="1", content="c", type=MemoryType.EPISODIC, created_at=time.time())
        s = self.policy.compute_strength(t, time.time())
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_episodic_decays_faster_than_semantic(self):
        now = time.time()
        old = now - 100 * 3600  # 100 小时前
        e = MemoryTrace(id="e", content="x", type=MemoryType.EPISODIC, created_at=old, last_accessed=old)
        s = MemoryTrace(id="s", content="x", type=MemoryType.SEMANTIC, created_at=old, last_accessed=old)
        self.assertLess(self.policy.compute_strength(e, now), self.policy.compute_strength(s, now))


class TestDecayMathInvariants(unittest.TestCase):
    """Ebbinghaus 衰减的数学不变量（对齐 Poirot test_decay.py 思路）。

    formula: strength = base × (1-rate)^time_hours + log(1+access_count)×0.1 + importance×0.05
    约束: strength ∈ [0,1]；纯函数不改 trace；config 可覆盖且需隔离。
    """

    def setUp(self):
        self.policy = EbbinghausDecayPolicy()
        # 保存全局 config，测试内覆盖后 tearDown 恢复，避免污染其他测试
        self._original_config = get_memory_config()

    def tearDown(self):
        set_memory_config(self._original_config)

    def _trace(self, *, type=MemoryType.EPISODIC, created_at=1000.0, last_accessed=0.0,
               access_count=0, importance=0.5) -> MemoryTrace:
        return MemoryTrace(
            id="t1", content="c", type=type,
            created_at=created_at, last_accessed=last_accessed,
            access_count=access_count, importance=importance,
        )

    def test_exact_formula_at_zero_hours(self):
        """time_hours=0：strength = base + 0 + importance×0.05。"""
        trace = self._trace(type=MemoryType.EPISODIC, created_at=1000.0, last_accessed=1000.0)
        s = self.policy.compute_strength(trace, 1000.0)
        expected = (
            DECAY_PARAMS["episodic"]["base_strength"]
            + 0.0
            + 0.5 * DECAY_COEFFICIENTS["importance_boost"]
        )
        self.assertAlmostEqual(s, expected, places=6)

    def test_time_increase_strength_decrease(self):
        """time_hours 单调增大 → strength 严格递减（衰减项递减）。"""
        trace = self._trace(last_accessed=1000.0)
        s0 = self.policy.compute_strength(trace, 1000.0)      # time_hours=0
        s1 = self.policy.compute_strength(trace, 1000.0 + 3600)   # time_hours=1
        s2 = self.policy.compute_strength(trace, 1000.0 + 3600 * 100)  # time_hours=100
        self.assertGreater(s0, s1)
        self.assertGreater(s1, s2)

    def test_type_ordering_after_time(self):
        """24h 后 strength：procedural > semantic > episodic（衰减率 0.005<0.02<0.1）。"""
        now = 1000.0 + 3600 * 24
        e = self._trace(type=MemoryType.EPISODIC, last_accessed=1000.0)
        s = self._trace(type=MemoryType.SEMANTIC, last_accessed=1000.0)
        p = self._trace(type=MemoryType.PROCEDURAL, last_accessed=1000.0)
        s_e = self.policy.compute_strength(e, now)
        s_s = self.policy.compute_strength(s, now)
        s_p = self.policy.compute_strength(p, now)
        self.assertGreater(s_p, s_s)
        self.assertGreater(s_s, s_e)

    def test_access_boost_exact_formula(self):
        """强化项 = log(1+access_count) × 0.1。"""
        trace = self._trace(access_count=2, importance=0.0, last_accessed=1000.0)
        s = self.policy.compute_strength(trace, 1000.0)
        expected = (
            DECAY_PARAMS["episodic"]["base_strength"]
            + math.log(1 + 2) * DECAY_COEFFICIENTS["access_boost"]
            + 0.0
        )
        self.assertAlmostEqual(s, expected, places=6)

    def test_access_count_increases_strength(self):
        """access_count 增大 → strength 增大。"""
        low = self._trace(access_count=0, last_accessed=1000.0)
        high = self._trace(access_count=20, last_accessed=1000.0)
        self.assertGreater(
            self.policy.compute_strength(high, 1000.0),
            self.policy.compute_strength(low, 1000.0),
        )

    def test_importance_increases_strength(self):
        """importance 增大 → strength 增大。"""
        low = self._trace(importance=0.0, last_accessed=1000.0)
        high = self._trace(importance=1.0, last_accessed=1000.0)
        self.assertGreater(
            self.policy.compute_strength(high, 1000.0),
            self.policy.compute_strength(low, 1000.0),
        )

    def test_strength_always_in_zero_one_range(self):
        """极端输入下 strength 仍 ∈ [0,1]。"""
        maxed = self._trace(access_count=1000, importance=1.0, last_accessed=1000.0)
        s_max = self.policy.compute_strength(maxed, 1000.0)
        # 完全衰减 + 零强化
        zero = self._trace(access_count=0, importance=0.0, last_accessed=1000.0)
        s_min = self.policy.compute_strength(zero, 1000.0 + 3600 * 100000)
        self.assertLessEqual(s_max, 1.0)
        self.assertGreaterEqual(s_min, 0.0)

    def test_pure_function_repeated_calls(self):
        """纯函数：多次调用结果一致 + trace 字段不被修改。"""
        trace = self._trace(access_count=5, importance=0.6, last_accessed=1000.0)
        s1 = self.policy.compute_strength(trace, 2000.0)
        s2 = self.policy.compute_strength(trace, 2000.0)
        self.assertEqual(s1, s2)
        self.assertEqual(trace.access_count, 5)
        self.assertEqual(trace.importance, 0.6)
        self.assertEqual(trace.last_accessed, 1000.0)
        self.assertEqual(trace.strength, 0.0)

    def test_unaccessed_uses_created_at(self):
        """last_accessed<=0 时用 created_at 算 time_hours。"""
        trace = self._trace(created_at=1000.0, last_accessed=0.0)
        s_now = self.policy.compute_strength(trace, 1000.0)      # time_hours=0
        s_later = self.policy.compute_strength(trace, 1000.0 + 3600)  # time_hours=1
        self.assertGreater(s_now, s_later)

    def test_config_override_and_restore(self):
        """set_memory_config 覆盖 decay 参数后生效，tearDown 恢复默认。"""
        trace = self._trace(type=MemoryType.EPISODIC, last_accessed=1000.0)
        s_default = self.policy.compute_strength(trace, 1000.0)

        new_config = replace(
            self._original_config,
            decay={"episodic": {"base_strength": 0.99, "decay_rate": 0.5}},
        )
        set_memory_config(new_config)
        s_override = self.policy.compute_strength(trace, 1000.0)
        self.assertGreater(s_override, s_default)  # base 0.99 > 0.7

        # tearDown 已恢复原 config；显式验证一次
        set_memory_config(self._original_config)
        s_restored = self.policy.compute_strength(trace, 1000.0)
        self.assertEqual(s_restored, s_default)


class TestForget(unittest.TestCase):
    def test_ttl_expired(self):
        policy = CompositeForgetPolicy()
        t = MemoryTrace(id="1", content="c", type=MemoryType.EPISODIC, created_at=time.time() - 800 * 3600)
        self.assertTrue(policy.should_forget(t, time.time()))

    def test_fresh_not_forgotten(self):
        policy = CompositeForgetPolicy()
        t = MemoryTrace(id="1", content="c", type=MemoryType.EPISODIC, created_at=time.time())
        self.assertFalse(policy.should_forget(t, time.time()))


class TestMarkdownFileStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarkdownFileStore(self.tmp.name)

    def test_add_get(self):
        t = MemoryTrace(id="a1", content="hello", type=MemoryType.EPISODIC)
        self.store.add(t)
        self.assertEqual(self.store.get("a1").content, "hello")

    def test_roundtrip_reload(self):
        t = MemoryTrace(id="a1", content="hello world", type=MemoryType.SEMANTIC, importance=0.8)
        self.store.add(t)
        reloaded = MarkdownFileStore(self.tmp.name)
        got = reloaded.get("a1")
        self.assertIsNotNone(got)
        self.assertEqual(got.content, "hello world")
        self.assertEqual(got.type, MemoryType.SEMANTIC)

    def test_update_and_remove(self):
        t = MemoryTrace(id="a1", content="old", type=MemoryType.EPISODIC)
        self.store.add(t)
        self.store.update(MemoryTrace(id="a1", content="new", type=MemoryType.EPISODIC))
        self.assertEqual(self.store.get("a1").content, "new")
        self.store.remove("a1")
        self.assertIsNone(self.store.get("a1"))


class TestHybridRetriever(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarkdownFileStore(self.tmp.name)
        self.decay = EbbinghausDecayPolicy()
        self.retriever = HybridRetriever(self.store, self.decay)
        # 真实装配：bootstrap._wrap_store 让 store 变更触发 retriever 增量索引
        from novamind.core.memory.bootstrap import _wrap_store

        _wrap_store(self.store, self.retriever)

    def test_retrieve_hits(self):
        self.store.add(MemoryTrace(id="m1", content="the user likes python programming", type=MemoryType.EPISODIC))
        results = self.retriever.retrieve(MemoryQuery(text="python programming"))
        self.assertTrue(any(r.trace.id == "m1" for r in results))

    def test_forgotten_filtered(self):
        self.store.add(MemoryTrace(id="m1", content="secret python", type=MemoryType.EPISODIC, metadata={"forgotten": True}))
        results = self.retriever.retrieve(MemoryQuery(text="secret python"))
        self.assertEqual(len(results), 0)

    def test_retrieve_reinforces(self):
        self.store.add(MemoryTrace(id="m1", content="python code", type=MemoryType.EPISODIC))
        self.retriever.retrieve(MemoryQuery(text="python code"))
        self.assertGreater(self.store.get("m1").access_count, 0)

    def test_chinese_tokenize_retrieves(self):
        """中文记忆用 jieba 分词，纯中文查询可命中。"""
        self.store.add(MemoryTrace(id="cn1", content="用户喜欢用 Python 做数据分析", type=MemoryType.PROCEDURAL))
        results = self.retriever.retrieve(MemoryQuery(text="数据分析"))
        self.assertTrue(any(r.trace.id == "cn1" for r in results))

    def test_english_still_space_tokenized(self):
        self.store.add(MemoryTrace(id="en1", content="deploy to production environment", type=MemoryType.EPISODIC))
        results = self.retriever.retrieve(MemoryQuery(text="production environment"))
        self.assertTrue(any(r.trace.id == "en1" for r in results))

    def test_mixed_cjk_english(self):
        self.store.add(MemoryTrace(id="mix1", content="使用 LangChain 构建 agent", type=MemoryType.PROCEDURAL))
        results = self.retriever.retrieve(MemoryQuery(text="LangChain"))
        self.assertTrue(any(r.trace.id == "mix1" for r in results))


class TestDefaultMemoryManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarkdownFileStore(self.tmp.name)
        self.decay = EbbinghausDecayPolicy()
        self.manager = DefaultMemoryManager(store=self.store, decay_policy=self.decay)

    def test_encode_idempotent(self):
        t1 = self.manager.encode("user likes coffee", MemoryType.EPISODIC)
        t2 = self.manager.encode("user likes coffee", MemoryType.EPISODIC)
        self.assertEqual(t1.id, t2.id)  # 同内容同 type 幂等

    def test_associate_bidirectional(self):
        a = self.manager.encode("fact a", MemoryType.SEMANTIC)
        b = self.manager.encode("fact b", MemoryType.SEMANTIC)
        self.manager.associate(a.id, b.id)
        self.assertTrue(any(x.target_id == b.id for x in self.store.get(a.id).associations))
        self.assertTrue(any(x.target_id == a.id for x in self.store.get(b.id).associations))

    def test_consolidate_marks_old_forgotten(self):
        a = self.manager.encode("note 1", MemoryType.EPISODIC)
        b = self.manager.encode("note 2", MemoryType.EPISODIC)
        merged = self.manager.consolidate([a.id, b.id], "merged knowledge")
        self.assertEqual(merged.type, MemoryType.SEMANTIC)
        self.assertTrue(self.store.get(a.id).metadata["forgotten"])
        self.assertTrue(self.store.get(b.id).metadata["forgotten"])

    def test_reconsolidate_preserves_strength(self):
        a = self.manager.encode("old content", MemoryType.SEMANTIC)
        orig_strength = a.strength
        updated = self.manager.reconsolidate(a.id, "new content")
        self.assertEqual(updated.id, a.id)
        self.assertEqual(updated.strength, orig_strength)
        self.assertEqual(updated.content, "new content")


class TestBuildDefaultProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_assemble(self):
        store = MarkdownFileStore(self.tmp.name)
        provider = build_default_provider(store=store)
        self.assertIsNotNone(provider.store())
        self.assertIsNotNone(provider.retriever())
        self.assertIsNotNone(provider.manager())


if __name__ == "__main__":
    unittest.main()
