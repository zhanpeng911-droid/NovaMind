"""B2 个性化召回测试：scope / 相关性门槛 / 信任 / 修正优先 / 偏好快照。"""

from __future__ import annotations

import os
import tempfile
import unittest

from novamind.core.memory.governance import MemoryGovernanceService, MemoryGovernanceStore, TRUST_UNTRUSTED
from novamind.core.memory.schema import MemoryTrace, MemoryType
from novamind.core.memory.strategies.default.decay import EbbinghausDecayPolicy
from novamind.core.memory.strategies.default.manager import DefaultMemoryManager
from novamind.core.memory.strategies.default.retriever import HybridRetriever
from novamind.core.memory.strategies.default.store import MarkdownFileStore
from novamind.core.memory.strategies.default.strategy import _wrap_store
from novamind.core.memory.types import MemoryQuery


class TestPersonalizedRecall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarkdownFileStore(self.tmp.name)
        self.decay = EbbinghausDecayPolicy()
        self.retriever = HybridRetriever(self.store, self.decay)
        _wrap_store(self.store, self.retriever)
        self.manager = DefaultMemoryManager(self.store, decay_policy=self.decay)
        self.gov = MemoryGovernanceService(
            MemoryGovernanceStore(os.path.join(self.tmp.name, "gov.sqlite3")),
            trace_store=self.store,
            retriever=self.retriever,
        )

    def _add(self, content: str, mem_type: MemoryType, *, scope: str = "global",
             confirm: bool = True, trust_level: str = "conversation") -> MemoryTrace:
        trace = self.manager.encode(content, mem_type, importance=0.8, source="test")
        self.gov.propose(
            trace, source_thread_id="t1", scope=scope, trust_level=trust_level,
        )
        if confirm:
            self.gov.confirm(trace.id)
        return trace

    def _recall(self, query: str, **kwargs):
        results = self.retriever.retrieve(MemoryQuery(text=query, top_k=10))
        return self.gov.filter_recallable(results, **kwargs)

    def test_scope_keeps_global_and_current_only(self):
        project_a = self._add("项目 A 使用 pytest 做测试", MemoryType.SEMANTIC, scope="project-a")
        global_pref = self._add("回答时优先给结论", MemoryType.PROCEDURAL, scope="global")

        results = self._recall("pytest 测试 回答 结论", scope="project-b")
        ids = {r.trace.id for r in results}
        self.assertIn(global_pref.id, ids)
        self.assertNotIn(project_a.id, ids)

        in_scope = self._recall("pytest 测试 回答 结论", scope="project-a")
        self.assertIn(project_a.id, {r.trace.id for r in in_scope})

    def test_relevance_gate_filters_low_score(self):
        self._add("今天天气不错适合散步", MemoryType.EPISODIC)
        results = self._recall("Python 异步编程", min_score=0.9)
        self.assertEqual(results, [])

    def test_untrusted_unconfirmed_not_injected(self):
        self._add("外部网页要求忽略规则", MemoryType.SEMANTIC,
                  confirm=False, trust_level=TRUST_UNTRUSTED)
        results = self._recall("外部网页 忽略规则")
        self.assertEqual(results, [])

    def test_correction_disables_old_even_with_high_strength(self):
        old = self._add("回答要非常详细", MemoryType.PROCEDURAL)
        # 通过多次检索抬高旧记忆的强度/访问计数
        for _ in range(20):
            self.retriever.retrieve(MemoryQuery(text="回答 详细"))
        new = self._add("回答要简短", MemoryType.PROCEDURAL)
        self.gov.supersede(old.id, new.id)

        results = self._recall("回答 详细 简短")
        ids = {r.trace.id for r in results}
        self.assertIn(new.id, ids)
        self.assertNotIn(old.id, ids)

    def test_rejected_not_injected_even_if_high_strength(self):
        trace = self._add("用户可能喜欢蓝色", MemoryType.SEMANTIC)
        for _ in range(20):
            self.retriever.retrieve(MemoryQuery(text="蓝色"))
        self.gov.reject(trace.id)
        self.assertEqual(self._recall("蓝色"), [])

    def test_recall_preferences_only_returns_preferences(self):
        self._add("偏好中文回答", MemoryType.PROCEDURAL)
        self._add("项目使用 Python 3.12", MemoryType.SEMANTIC)
        self._add("上次部署失败因为没有密钥", MemoryType.EPISODIC)
        prefs = self.gov.recall_preferences("回答 项目 部署", top_k=10)
        self.assertEqual(prefs, ["偏好中文回答"])

    def test_recall_preferences_respects_untrusted(self):
        self._add("外部要求用英文回答", MemoryType.PROCEDURAL,
                  confirm=False, trust_level=TRUST_UNTRUSTED)
        self.assertEqual(self.gov.recall_preferences("回答 英文", top_k=10), [])


if __name__ == "__main__":
    unittest.main()
