"""
技能自进化链路测试（P1 覆盖率盲区补齐，LLM 全部 mock）。

覆盖关键不变量：
  - MetricMonitorTrigger：阈值边界（fallback_rate 刚跌破/刚高于 0.4、低完成+高应用）、
    disabled / min_selections / cooldown 过滤、mark_evolved 冷却重置、LLM 确认 yes/no
  - IVEFocuser：无证据透传、无 LLM 降级摘要、IMPLEMENTATION 计数升级 FUNDAMENTAL、
    FUNDAMENTAL 清零计数、畸形 LLM 输出回退、judgment notes 容错
  - LLMMutator：FIX 编辑 + 预算裁剪、CAPTURED 生成 + 名字校验、无 LLM 行为
  - ScoreDeltaGate：hard_failures 拒绝、CAPTURED 接受/拒绝、delta 门边界
  - EvolutionManager：run_cycle 编排闭环（accept 建版本 / reject 不建）、
    create_version 异常容错、evolve_skill 未知名报错
"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from novamind.core.skill.evolution.focus.ive_focuser import IVEFocuser
from novamind.core.skill.evolution.gates.score_delta_gate import ScoreDeltaGate
from novamind.core.skill.evolution.manager import EvolutionManager
from novamind.core.skill.evolution.mutators.llm_mutator import LLMMutator
from novamind.core.skill.evolution.triggers.metric_monitor import MetricMonitorTrigger
from novamind.core.skill.evolution.types import (
    EvalContext,
    EvalResult,
    EvolutionContext,
    GateDecision,
)
from novamind.core.skill.types import SkillLineage, SkillRecord


def _skill(name="demo", selections=10, applied=8, completions=2, fallbacks=6,
           enabled=True, skill_id="s1", path="/tmp/x.md") -> SkillRecord:
    return SkillRecord(
        skill_id=skill_id, name=name, path=path, content_hash="h",
        lineage=SkillLineage(origin="IMPORTED"),
        description="d", enabled=enabled,
        total_selections=selections, total_applied=applied,
        total_completions=completions, total_fallbacks=fallbacks,
    )


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class TestMetricMonitorTrigger(unittest.TestCase):
    def setUp(self):
        self.trigger = MetricMonitorTrigger(min_selections=5, cooldown_turns=10)

    def _store(self, *records):
        store = MagicMock()
        store.list_active.return_value = list(records)
        return store

    def test_healthy_skill_not_triggered(self):
        """健康指标（低 fallback、高完成）不触发。"""
        rec = _skill(selections=10, applied=9, completions=8, fallbacks=1)
        self.assertEqual(self.trigger.should_trigger(self._store(rec)), [])

    def test_disabled_skill_skipped(self):
        rec = _skill(enabled=False, fallbacks=9, selections=10, applied=1)
        self.assertEqual(self.trigger.should_trigger(self._store(rec)), [])

    def test_below_min_selections_skipped(self):
        rec = _skill(selections=3, fallbacks=3)  # fallback 100% 但选择数不足
        self.assertEqual(self.trigger.should_trigger(self._store(rec)), [])

    def test_high_fallback_rate_triggers_with_direction(self):
        """fallback_rate > 0.4 触发，方向含高 fallback 描述。"""
        rec = _skill(selections=10, applied=2, completions=1, fallbacks=5)  # 50%
        results = self.trigger.should_trigger(self._store(rec))
        self.assertEqual(len(results), 1)
        self.assertIn("fallback_rate", results[0].fix_direction)

    def test_fallback_at_exact_threshold_not_triggered(self):
        """边界：fallback_rate == 0.4（4/10）不触发（条件是严格大于）。"""
        rec = _skill(selections=10, applied=6, completions=6, fallbacks=4)
        self.assertEqual(self.trigger.should_trigger(self._store(rec)), [])

    def test_low_completion_high_applied_triggers(self):
        rec = _skill(selections=10, applied=10, completions=2, fallbacks=0)
        results = self.trigger.should_trigger(self._store(rec))
        self.assertEqual(len(results), 1)
        self.assertIn("completion_rate", results[0].fix_direction)

    def test_cooldown_blocks_then_mark_evolved_resets(self):
        rec = _skill(selections=20, applied=2, completions=1, fallbacks=15)
        self.trigger.mark_evolved(rec.name, 15)  # 最近一次在 selections=15
        # 20-15=5 < cooldown 10 → 冷却中
        self.assertEqual(self.trigger.should_trigger(self._store(rec)), [])
        # 越过冷却窗口后恢复触发
        evolved = replace(rec, total_selections=30)
        results = self.trigger.should_trigger(self._store(evolved))
        self.assertEqual(len(results), 1)

    def _llm(self, reply: str) -> MagicMock:
        mock = MagicMock()
        mock.invoke.return_value = _FakeResp(reply)
        return mock

    def test_llm_confirm_no_blocks_trigger(self):
        llm = self._llm("no")
        trigger = MetricMonitorTrigger(llm=llm, min_selections=5, cooldown_turns=10)
        rec = _skill(selections=10, applied=2, completions=1, fallbacks=5)
        self.assertEqual(trigger.should_trigger(self._store(rec)), [])
        self.assertTrue(llm.invoke.called)

    def test_llm_confirm_yes_allows_trigger(self):
        llm = self._llm("yes")
        trigger = MetricMonitorTrigger(llm=llm, min_selections=5, cooldown_turns=10)
        rec = _skill(selections=10, applied=2, completions=1, fallbacks=5)
        self.assertEqual(len(trigger.should_trigger(self._store(rec))), 1)


class _NoJudgmentStore:
    def get_judgments(self, skill_id, limit=10):
        raise RuntimeError("no judgments table")


def _ctx_with_evidence(skill=None):
    from novamind.core.skill.evolution.types import FailureEvidence
    return EvolutionContext(
        trigger="METRIC", evolution_type="FIX",
        target_skill=skill or _skill(),
        fix_direction="初始方向",
        failure_evidence=(FailureEvidence(turn_index=1, tool_name="t",
                                          failure_class="UNKNOWN",
                                          description="执行失败"),),
    )


class TestIVEFocuser(unittest.TestCase):
    def test_no_evidence_passthrough(self):
        focuser = IVEFocuser()
        ctx = EvolutionContext(trigger="METRIC", evolution_type="FIX", target_skill=_skill())
        self.assertIs(focuser.focus(ctx, None), ctx)

    def test_degrade_focus_without_llm(self):
        focuser = IVEFocuser(llm=None)
        out = focuser.focus(_ctx_with_evidence(), None)
        self.assertIn("降级全量摘要", out.fix_direction)
        self.assertIn("执行失败", out.fix_direction)

    def _llm_reply(self, reply: str) -> MagicMock:
        mock = MagicMock()
        mock.invoke.return_value = _FakeResp(reply)
        return mock

    def test_llm_implementation_increments_counter(self):
        llm = self._llm_reply('{"class": "IMPLEMENTATION", "direction": "改步骤"}')
        focuser = IVEFocuser(llm=llm)
        out = focuser.focus(_ctx_with_evidence(), None)
        self.assertIn("改步骤", out.fix_direction)
        self.assertEqual(out.failure_evidence[0].failure_class, "IMPLEMENTATION")
        self.assertEqual(focuser._impl_fail_counts["demo"], 1)

    def test_impl_threshold_upgrades_to_fundamental(self):
        llm = self._llm_reply('{"class": "IMPLEMENTATION", "direction": "d"}')
        focuser = IVEFocuser(llm=llm, impl_fail_threshold=2)
        focuser.focus(_ctx_with_evidence(), None)
        out = focuser.focus(_ctx_with_evidence(), None)
        self.assertEqual(out.failure_evidence[0].failure_class, "FUNDAMENTAL")
        self.assertIn("[升级 fundamental]", out.fix_direction)

    def test_fundamental_resets_counter(self):
        llm = MagicMock()
        llm.invoke.side_effect = [
            _FakeResp('{"class": "IMPLEMENTATION", "direction": "x"}'),
            _FakeResp('{"class": "FUNDAMENTAL", "direction": "重写"}'),
        ]
        focuser = IVEFocuser(llm=llm)
        focuser.focus(_ctx_with_evidence(), None)
        self.assertEqual(focuser._impl_fail_counts.get("demo"), 1)
        focuser.focus(_ctx_with_evidence(), None)
        self.assertEqual(focuser._impl_fail_counts.get("demo"), 0)

    def test_malformed_llm_output_falls_back_to_implementation(self):
        llm = self._llm_reply("not json at all")
        focuser = IVEFocuser(llm=llm)
        out = focuser.focus(_ctx_with_evidence(), None)
        self.assertEqual(out.failure_evidence[0].failure_class, "IMPLEMENTATION")

    def test_store_judgment_failure_tolerated(self):
        llm = self._llm_reply('{"class": "IMPLEMENTATION"}')
        focuser = IVEFocuser(llm=llm)
        out = focuser.focus(_ctx_with_evidence(), _NoJudgmentStore())
        self.assertIsNotNone(out.fix_direction)


_SKILL_MD = """---
name: demo
description: d
---

# Steps
do thing
"""


class TestLLMMutator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.skill_path = Path(self.tmp.name) / "demo.md"
        self.skill_path.write_text(_SKILL_MD, encoding="utf-8")

    def test_mutate_fix_edits_body_and_writes_staging(self):
        mutator = LLMMutator()
        llm = MagicMock()
        llm.invoke.return_value = _FakeResp("# Steps\ndo thing better\n")
        baseline = _skill(path=str(self.skill_path))
        candidate, diff = mutator.mutate(_ctx_with_evidence(baseline), llm)
        self.assertTrue(candidate.skill_id.startswith("demo__cand_"))
        self.assertFalse(candidate.is_active)
        self.assertEqual(candidate.lineage.origin, "FIXED")
        self.assertEqual(candidate.lineage.parent_skill_ids, ("s1",))
        self.assertIn("better", diff or "")
        staged = Path(candidate.path)
        self.addCleanup(lambda: staged.unlink(missing_ok=True))
        self.assertIn("better", staged.read_text(encoding="utf-8"))

    def test_mutate_fix_without_llm_keeps_body(self):
        mutator = LLMMutator()
        baseline = _skill(path=str(self.skill_path))
        candidate, _ = mutator.mutate(_ctx_with_evidence(baseline), None)
        self.assertTrue(Path(candidate.path).exists())
        self.addCleanup(lambda: Path(candidate.path).unlink(missing_ok=True))

    def test_mutate_capture_generates_new_skill(self):
        mutator = LLMMutator()
        md = "---\nname: new-skill\ndescription: 新技能\n---\nbody"
        llm = MagicMock()
        llm.invoke.return_value = _FakeResp(md)
        ctx = EvolutionContext(trigger="CAPTURE", evolution_type="CAPTURED",
                               target_skill=None, capture_pattern="p",
                               suggested_name="new-skill")
        candidate, diff = mutator.mutate(ctx, llm)
        self.assertEqual(candidate.name, "new-skill")
        self.assertEqual(candidate.lineage.origin, "CAPTURED")
        self.assertIn("CAPTURED", diff)
        self.addCleanup(lambda: Path(candidate.path).unlink(missing_ok=True))

    def test_capture_rejects_invalid_name(self):
        mutator = LLMMutator()
        md = "---\nname: Bad Name!\ndescription: x\n---\nbody"
        llm = MagicMock()
        llm.invoke.return_value = _FakeResp(md)
        ctx = EvolutionContext(trigger="CAPTURE", evolution_type="CAPTURED",
                               target_skill=None, capture_pattern="p",
                               suggested_name="whatever")
        with self.assertRaises(ValueError):
            mutator.mutate(ctx, llm)

    def test_capture_without_llm_raises(self):
        mutator = LLMMutator()
        ctx = EvolutionContext(trigger="CAPTURE", evolution_type="CAPTURED",
                               target_skill=None, capture_pattern="p",
                               suggested_name="s")
        with self.assertRaises(ValueError):
            mutator.mutate(ctx, None)

    def test_unsupported_evolution_type_raises(self):
        mutator = LLMMutator()
        ctx = EvolutionContext(trigger="METRIC", evolution_type="UNKNOWN",
                               target_skill=_skill())
        with self.assertRaises(ValueError):
            mutator.mutate(ctx, None)


class TestScoreDeltaGate(unittest.TestCase):
    def setUp(self):
        self.gate = ScoreDeltaGate()

    def _eval(self, score, hard=(), evidence=()):
        return EvalResult(score=score, hard_failures=tuple(hard), evidence=tuple(evidence))

    def test_hard_failures_reject_even_with_high_score(self):
        decision = self.gate.decide(_skill(), _skill(), self._eval(0.99, hard=("crash",)))
        self.assertEqual(decision.recommendation, "reject")

    def test_captured_accept_when_score_positive(self):
        decision = self.gate.decide(_skill(), None, self._eval(0.7))
        self.assertEqual(decision.recommendation, "accept")

    def test_captured_reject_when_score_zero(self):
        decision = self.gate.decide(_skill(), None, self._eval(0.0))
        self.assertEqual(decision.recommendation, "reject")

    @staticmethod
    def _evidences(base_pass: list[bool]):
        from novamind.core.skill.evolution.types import EvalEvidence
        return tuple(
            EvalEvidence(kind="rule", rule_name=f"r{i}", baseline_pass=b,
                         candidate_pass=True)
            for i, b in enumerate(base_pass)
        )

    def test_accept_above_baseline_plus_delta(self):
        evidence = self._evidences([True, False])  # baseline_score = 0.5
        decision = self.gate.decide(_skill(), _skill(), self._eval(0.8, evidence=evidence))
        self.assertEqual(decision.recommendation, "accept")

    def test_reject_at_or_below_baseline_plus_delta(self):
        evidence = self._evidences([True, False])  # 0.5
        decision = self.gate.decide(_skill(), _skill(), self._eval(0.5, evidence=evidence))
        self.assertEqual(decision.recommendation, "reject")

    def test_strict_delta_gate_boundary(self):
        gate = ScoreDeltaGate(min_delta=0.2)
        evidence = self._evidences([True])  # baseline 1.0
        # 1.15 <= 1.0+0.2 → reject；1.21 > 1.2 → accept
        self.assertEqual(gate.decide(_skill(), _skill(), self._eval(1.15, evidence=evidence)).recommendation, "reject")
        self.assertEqual(gate.decide(_skill(), _skill(), self._eval(1.21, evidence=evidence)).recommendation, "accept")


class _StubTrigger:
    def __init__(self, contexts):
        self._contexts = contexts
        self.evolved: list[str] = []

    def should_trigger(self, store):
        return list(self._contexts)

    def mark_evolved(self, name, selections):
        self.evolved.append(name)


class _StubFocuser:
    def focus(self, ctx, store):
        return ctx


class _StubMutator:
    def __init__(self, candidate):
        self._candidate = candidate

    def mutate(self, ctx, llm):
        return self._candidate, "+1 line"


class _StubEvalBridge:
    def __init__(self, result):
        self._result = result

    def evaluate(self, eval_ctx):
        return self._result


class _StubGate:
    def __init__(self, recommendation):
        self._rec = recommendation

    def decide(self, candidate, baseline, eval_result):
        return GateDecision(recommendation=self._rec, reason="stub")


class _StubStore:
    def __init__(self):
        self.created: list[tuple] = []
        self.recorded: list = []

    def get_active(self, name):
        return _skill(name=name)

    def get_metrics(self, skill_id):
        return None

    def create_version(self, parent_id, candidate, origin):
        self.created.append((parent_id, candidate.name))
        return "v_new"

    def record_evolution(self, rec):
        self.recorded.append(rec)


class TestEvolutionManagerOrchestration(unittest.TestCase):
    def _manager(self, store, recommendation="accept"):
        cand = _skill(skill_id="cand1")
        return (EvolutionManager(
            store=store,
            triggers=[],
            focuser=_StubFocuser(),
            mutator=_StubMutator(cand),
            eval_bridge=_StubEvalBridge(EvalResult(score=0.9)),
            gate=_StubGate(recommendation),
        ), cand)

    def test_run_cycle_accept_creates_version_and_records(self):
        store = _StubStore()
        manager, cand = self._manager(store, "accept")
        manager._triggers = [_StubTrigger([
            EvolutionContext(trigger="METRIC", evolution_type="FIX",
                             target_skill=_skill(), fix_direction="fix it"),
        ])]
        records = manager.run_cycle()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].gate_decision, "accept")
        self.assertEqual(store.created, [("s1", "demo")])
        self.assertEqual(store.recorded, records)
        self.assertEqual(manager._triggers[0].evolved, ["demo"])

    def test_run_cycle_reject_records_but_no_version(self):
        store = _StubStore()
        manager, _ = self._manager(store, "reject")
        manager._triggers = [_StubTrigger([
            EvolutionContext(trigger="METRIC", evolution_type="FIX",
                             target_skill=_skill(), fix_direction="f"),
        ])]
        records = manager.run_cycle()
        self.assertEqual(len(records), 1)
        self.assertEqual(store.created, [])
        self.assertIsNone(records[0].created_version_id)

    def test_run_cycle_create_version_failure_still_records(self):
        store = _StubStore()

        def boom(parent_id, candidate, origin):
            raise RuntimeError("disk full")

        store.create_version = boom
        manager, _ = self._manager(store, "accept")
        manager._triggers = [_StubTrigger([
            EvolutionContext(trigger="METRIC", evolution_type="FIX",
                             target_skill=_skill(), fix_direction="f"),
        ])]
        records = manager.run_cycle()
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].created_version_id)

    def test_evolve_skill_unknown_raises_valueerror(self):
        store = MagicMock()
        store.get_active.return_value = None
        manager, _ = self._manager(store)
        with self.assertRaises(ValueError):
            manager.evolve_skill("ghost")


if __name__ == "__main__":
    unittest.main()


class TestGitRatchet(unittest.TestCase):
    """上线后退化自动回滚（棘轮）判定。"""

    def setUp(self):
        from novamind.core.skill.evolution.gates.git_ratchet import GitRatchet
        self.gate = GitRatchet(degradation_threshold=0.3, min_selections=5)

    def _store(self, versions):
        store = MagicMock()
        store.get_versions.return_value = versions
        return store

    def test_below_min_selections_no_rollback(self):
        current = _skill(selections=4, applied=0, completions=0)
        self.assertIsNone(self.gate.check_and_rollback(self._store([]), current))

    def test_healthy_effective_rate_no_rollback(self):
        current = _skill(selections=10, completions=8)  # effective 0.8 >= 0.3
        self.assertIsNone(self.gate.check_and_rollback(self._store([_skill()]), current))

    def test_degraded_prefers_parent_version(self):
        parent = _skill(skill_id="parent", selections=1, completions=1)
        child = _skill(skill_id="child", selections=10, completions=1)
        current = replace(child,
                          lineage=SkillLineage(parent_skill_ids=("parent",), generation=1))
        store = self._store([current, parent])
        target = self.gate.check_and_rollback(store, current)
        self.assertEqual(target, "parent")
        store.rollback.assert_called_once_with("parent")

    def test_fallback_to_lowest_generation_when_no_parent_in_versions(self):
        gen0 = replace(_skill(skill_id="g0"), lineage=SkillLineage(generation=0))
        gen2 = replace(_skill(skill_id="g2"), lineage=SkillLineage(generation=2))
        current = _skill(skill_id="cur", selections=10, completions=1)
        target = self.gate.check_and_rollback(self._store([gen2, gen0]), current)
        self.assertEqual(target, "g0")  # min generation

    def test_only_self_in_versions_no_rollback(self):
        current = _skill(skill_id="cur", selections=10, completions=1)
        target = self.gate.check_and_rollback(self._store([current]), current)
        self.assertIsNone(target)
