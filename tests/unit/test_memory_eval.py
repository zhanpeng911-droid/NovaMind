"""B3 记忆评测 harness（确定性，不依赖真实模型）。

指标：
- 偏好遵循（期望记忆被召回）>= 90%
- 干扰查询误注入 = 0
- 跨会话召回 = 10/10
- 删除/拒绝/禁用/在飞竞态注入 = 0（20/20）
"""

from __future__ import annotations

import os
import tempfile
import unittest

from novamind.core.memory.governance import (
    GovernanceError,
    MemoryGovernanceService,
    MemoryGovernanceStore,
)
from novamind.core.memory.schema import MemoryType
from novamind.core.memory.strategies.default.decay import EbbinghausDecayPolicy
from novamind.core.memory.strategies.default.manager import DefaultMemoryManager
from novamind.core.memory.strategies.default.retriever import HybridRetriever
from novamind.core.memory.strategies.default.store import MarkdownFileStore
from novamind.core.memory.strategies.default.strategy import _wrap_store
from novamind.core.memory.types import MemoryQuery

from fixtures.memory_eval import (
    CROSS_SESSION,
    DISTRACTORS,
    PREFERENCES,
    RACE_CASES,
)


class TestMemoryEvaluation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "gov.sqlite3")
        self.store = MarkdownFileStore(self.tmp.name)
        self.decay = EbbinghausDecayPolicy()
        self.retriever = HybridRetriever(self.store, self.decay)
        _wrap_store(self.store, self.retriever)
        self.manager = DefaultMemoryManager(self.store, decay_policy=self.decay)
        self.gov = MemoryGovernanceService(
            MemoryGovernanceStore(self.db_path),
            trace_store=self.store,
            retriever=self.retriever,
        )
        self._counter = 0

    def _add(self, content: str, *, source: str = "t", scope: str = "global",
             confirm: bool = True) -> str:
        self._counter += 1
        trace = self.manager.encode(
            content, MemoryType.PROCEDURAL, importance=0.8,
            source=f"{source}:{self._counter}",
        )
        self.gov.propose(trace, source_thread_id=source, scope=scope)
        if confirm:
            self.gov.confirm(trace.id)
        return trace.id

    def _recall_ids(self, query: str, *, scope: str | None = None,
                    min_score: float = 0.0) -> set[str]:
        results = self.retriever.retrieve(MemoryQuery(text=query, top_k=10))
        return {
            r.trace.id
            for r in self.gov.filter_recallable(
                results, scope=scope, min_score=min_score,
            )
        }

    # ── 30 条偏好 + 30 条干扰 ────────────────────────────────────────

    def test_preference_adherence_and_distractor_leaks(self):
        expected = {}
        for content, _ in PREFERENCES:
            expected[content] = self._add(content)

        hits = 0
        for content, query in PREFERENCES:
            if expected[content] in self._recall_ids(query):
                hits += 1
        adherence = hits / len(PREFERENCES)

        all_ids = set(expected.values())
        leaks = 0
        for query in DISTRACTORS:
            # 干扰查询用相关性门槛过滤弱匹配（生产召回默认 min_score=0.05）
            if self._recall_ids(query, min_score=0.1) & all_ids:
                leaks += 1

        print(f"[B3] preference adherence={hits}/{len(PREFERENCES)} "
              f"distractor leaks={leaks}/{len(DISTRACTORS)}")
        self.assertGreaterEqual(adherence, 0.90)
        self.assertEqual(leaks, 0)

    # ── 10 组跨会话 ─────────────────────────────────────────────────

    def test_cross_session_recall(self):
        passed = 0
        for content, query, source in CROSS_SESSION:
            memory_id = self._add(content, source=source)
            if memory_id in self._recall_ids(query):
                passed += 1
        print(f"[B3] cross-session={passed}/{len(CROSS_SESSION)}")
        self.assertEqual(passed, len(CROSS_SESSION))

    # ── 20 组删除/拒绝/禁用/在飞竞态 ────────────────────────────────

    def test_race_cases_never_inject(self):
        passed = 0
        injection = 0
        for index, (action, expected_recallable) in enumerate(RACE_CASES):
            memory_id = self._add(f"竞态记忆 {index} {action} 用例", source="race")
            recallable = self._apply_race(action, memory_id)
            if recallable == expected_recallable:
                passed += 1
            if expected_recallable is False and recallable is True:
                injection += 1
        print(f"[B3] race pass={passed}/{len(RACE_CASES)} injection={injection}")
        self.assertEqual(injection, 0, "删除/拒绝/禁用后仍可召回")
        self.assertEqual(passed, len(RACE_CASES))

    def _apply_race(self, action: str, memory_id: str) -> bool:
        if action == "delete":
            self.gov.delete(memory_id)
        elif action == "delete_then_migrate":
            self.gov.delete(memory_id)
            self.gov.migrate_legacy(self.store.list_all())
        elif action == "delete_then_new_service":
            self.gov.delete(memory_id)
            fresh = MemoryGovernanceService(
                MemoryGovernanceStore(self.db_path),
                trace_store=self.store,
                retriever=self.retriever,
            )
            fresh.migrate_legacy(self.store.list_all())
            return fresh.is_recallable(memory_id)
        elif action == "disable":
            self.gov.disable(memory_id)
        elif action == "disable_then_enable":
            self.gov.disable(memory_id)
            self.gov.enable(memory_id)
        elif action == "disable_then_supersede":
            self.gov.disable(memory_id)
            replacement = self._add("竞态记忆 替代值", source="race")
            self.gov.supersede(memory_id, replacement)
        elif action == "disable_then_migrate":
            self.gov.disable(memory_id)
            self.gov.migrate_legacy(self.store.list_all())
        elif action == "reject":
            self.gov.reject(memory_id)
        elif action == "reject_then_propose":
            self.gov.reject(memory_id)
            with self.assertRaises(GovernanceError):
                trace = self.store.get(memory_id)
                self.gov.propose(trace, source_thread_id="race")
        elif action == "reject_then_migrate":
            self.gov.reject(memory_id)
            self.gov.migrate_legacy(self.store.list_all())
        elif action == "reject_then_confirm":
            self.gov.reject(memory_id)
            with self.assertRaises(GovernanceError):
                self.gov.confirm(memory_id)
        elif action == "worker_inflight_delete":
            self.gov.delete(memory_id)
            self.assertTrue(self.gov.blocked_for_worker(memory_id))
        elif action == "worker_inflight_reject":
            self.gov.reject(memory_id)
            self.assertTrue(self.gov.blocked_for_worker(memory_id))
        elif action == "worker_inflight_disable":
            self.gov.disable(memory_id)
            self.assertTrue(self.gov.blocked_for_worker(memory_id))
        else:
            raise AssertionError(f"unknown race action: {action}")
        return self.gov.is_recallable(memory_id)


if __name__ == "__main__":
    unittest.main()
