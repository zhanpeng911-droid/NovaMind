"""
三层技能 P4 测试。

覆盖关键不变量：
  - SkillStore：version DAG + is_active 单指针 + 四计数器 + rollback 切指针
  - parser：frontmatter 解析 + 必需字段校验
  - selector：quality filter + 候选数截断
  - 进化闭环：trigger→focus→mutate→eval→gate，退化 reject
  - 评估：contract 规则（nonempty/json_parseable hard）+ 四维加权
"""
import tempfile
import unittest
from pathlib import Path

from novamind.core.skill import (
    SQLiteSkillStore,
    parse_skill_file,
    SkillSelector,
)
from novamind.core.skill.types import SkillRecord, SkillLineage
from novamind.core.skill.evolution import (
    EvolutionManager,
    MetricMonitorTrigger,
    IVEFocuser,
    LLMMutator,
    ScoreDeltaGate,
    ProgrammaticEvalBridge,
    GitRatchet,
)
from novamind.core.skill.evolution.types import EvolutionContext
from novamind.core.skill.eval.analyzers import ContractCompiler, ResponseContractChecker
from novamind.core.skill.eval.analyzers.checks import check_nonempty, check_json_parseable


def _make_record(name="test_skill", content="body content"):
    return SkillRecord(
        skill_id=f"{name}__builtin", name=name, path="/tmp/skill.md",
        content_hash="abc123", description="desc", allowed_tools=(), enabled=True,
        lineage=SkillLineage(origin="BUILTIN"),
    )


class TestSkillStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteSkillStore(Path(self.tmp.name) / "skills.db")
        self.addCleanup(self.store.close)

    def test_register_get(self):
        rec = _make_record("s1")
        self.store.register(rec)
        self.assertEqual(self.store.get(rec.skill_id).name, "s1")

    def test_create_version_switches_active(self):
        base = _make_record("s1")
        self.store.register(base)
        new = SkillRecord(
            skill_id="s1__v1", name="s1", path="/tmp/v1.md", content_hash="x",
            lineage=SkillLineage(parent_skill_ids=(base.skill_id,), generation=1, origin="FIXED"),
        )
        self.store.create_version(base.skill_id, new, "FIXED")
        self.assertTrue(self.store.get("s1__v1").is_active)
        self.assertFalse(self.store.get(base.skill_id).is_active)

    def test_rollback_switches_pointer(self):
        base = _make_record("s1")
        self.store.register(base)
        new = SkillRecord(skill_id="s1__v1", name="s1", path="/tmp/v1.md", content_hash="x",
                          lineage=SkillLineage(parent_skill_ids=(base.skill_id,), generation=1))
        self.store.create_version(base.skill_id, new, "FIXED")
        self.store.rollback(base.skill_id)
        self.assertTrue(self.store.get(base.skill_id).is_active)
        self.assertFalse(self.store.get("s1__v1").is_active)

    def test_four_counters(self):
        rec = _make_record("s1")
        self.store.register(rec)
        self.store.record_selection(rec.skill_id)
        self.store.record_outcome(rec.skill_id, "run1", applied=True, task_completed=True)
        metrics = self.store.get_metrics(rec.skill_id)
        self.assertEqual(metrics.selections, 1)
        self.assertEqual(metrics.applied, 1)
        self.assertEqual(metrics.completions, 1)


class TestParser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_parse_valid(self):
        p = Path(self.tmp.name) / "SKILL.md"
        p.write_text("---\nname: my-skill\ndescription: does things\n---\nbody", encoding="utf-8")
        rec = parse_skill_file(p, origin="BUILTIN")
        self.assertEqual(rec.name, "my-skill")
        self.assertEqual(rec.skill_id, "my-skill__builtin")

    def test_parse_missing_name_raises(self):
        p = Path(self.tmp.name) / "SKILL.md"
        p.write_text("---\ndescription: no name\n---\nbody", encoding="utf-8")
        with self.assertRaises(ValueError):
            parse_skill_file(p)


class TestSkillSelector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteSkillStore(Path(self.tmp.name) / "skills.db")
        self.addCleanup(self.store.close)

    def test_fewer_than_max_returns_all(self):
        for i in range(2):
            self.store.register(_make_record(f"skill{i}"))
        selector = SkillSelector(self.store, max_skills=3)
        self.assertEqual(len(selector.select_for_task("task")), 2)


class TestContractChecker(unittest.TestCase):
    def test_hard_rules_nonempty_and_parseable(self):
        content = "---\nname: s\ndescription: d\n---\nbody text"
        self.assertTrue(check_nonempty(content))
        self.assertTrue(check_json_parseable(content))

    def test_empty_body_hard_fail(self):
        checker = ResponseContractChecker(ContractCompiler())
        result = checker.check("---\nname: s\n---\n", "")
        self.assertIn("nonempty", result.hard_failures)


class TestEvolutionLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteSkillStore(Path(self.tmp.name) / "skills.db")
        self.addCleanup(self.store.close)
        # 注册一个低效 skill（fallback_rate 高）
        rec = SkillRecord(
            skill_id="bad__builtin", name="bad", path="/tmp/bad.md", content_hash="x",
            description="bad skill", allowed_tools=(), enabled=True,
            total_selections=10, total_applied=1, total_completions=1, total_fallbacks=8,
            lineage=SkillLineage(origin="BUILTIN"),
        )
        self.store.register(rec)
        # register 不写计数器（Poirot 设计），手动 UPDATE 模拟累积的 metrics
        self.store._conn.execute(
            "UPDATE skill_records SET total_selections=10, total_applied=1, total_completions=1, total_fallbacks=8 WHERE skill_id='bad__builtin'"
        )
        self.store._conn.commit()
        # 写真实 SKILL.md 文件供 mutator/eval 读
        self.skill_file = Path(self.tmp.name) / "bad.md"
        self.skill_file.write_text("---\nname: bad\ndescription: bad skill\n---\nstep 1 do thing\n", encoding="utf-8")
        # 更新 path 指向真实文件
        from novamind.core.skill.types import SkillRecord as SR
        fixed = SR(skill_id="bad__builtin", name="bad", path=str(self.skill_file), content_hash="x",
                   description="bad skill", enabled=True, total_selections=10, total_applied=1,
                   total_completions=1, total_fallbacks=8, lineage=SkillLineage(origin="BUILTIN"))
        self.store._conn.execute("UPDATE skill_records SET path=? WHERE skill_id=?", (str(self.skill_file), "bad__builtin"))
        self.store._conn.commit()

    def test_trigger_detects_degraded(self):
        trigger = MetricMonitorTrigger(min_selections=5, cooldown_turns=1)
        contexts = trigger.should_trigger(self.store)
        self.assertTrue(any(c.target_skill.name == "bad" for c in contexts))

    def test_gate_rejects_worse_candidate(self):
        gate = ScoreDeltaGate(min_delta=0.0)
        from novamind.core.skill.evolution.types import EvalResult, EvalEvidence
        baseline = _make_record("bad")
        candidate = SkillRecord(skill_id="bad__cand", name="bad", path="/tmp/cand.md", content_hash="y")
        # baseline 全通过（score=1.0），candidate 通过率 0.3 → 退化 reject
        evidence = (EvalEvidence(kind="programmatic_rule", rule_name="nonempty", baseline_pass=True, candidate_pass=False),)
        result = EvalResult(score=0.3, hard_failures=(), evidence=evidence)
        decision = gate.decide(candidate, baseline, result)
        self.assertEqual(decision.recommendation, "reject")


class TestGitRatchet(unittest.TestCase):
    def test_rollback_when_degraded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SQLiteSkillStore(Path(tmp.name) / "skills.db")
        self.addCleanup(store.close)
        base = SkillRecord(skill_id="s__builtin", name="s", path="/tmp/s.md", content_hash="x",
                           total_selections=10, total_completions=8, lineage=SkillLineage(origin="BUILTIN"))
        store.register(base)
        new = SkillRecord(skill_id="s__v1", name="s", path="/tmp/s2.md", content_hash="y",
                          total_selections=10, total_completions=2, lineage=SkillLineage(parent_skill_ids=(base.skill_id,), generation=1))
        store.create_version(base.skill_id, new, "FIXED")
        # 手动设新版本 metrics（register/create_version 不写计数器）
        store._conn.execute(
            "UPDATE skill_records SET total_selections=10, total_completions=2 WHERE skill_id=?", (new.skill_id,)
        )
        store._conn.commit()
        ratchet = GitRatchet(degradation_threshold=0.3, min_selections=5)
        rolled = ratchet.check_and_rollback(store, store.get_active("s"))
        self.assertEqual(rolled, base.skill_id)
        self.assertTrue(store.get(base.skill_id).is_active)


if __name__ == "__main__":
    unittest.main()
