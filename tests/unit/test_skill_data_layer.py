"""
技能数据层测试（P1-B1：store truth source / selector 混合选择 / parser 容错 / eval checks）。

覆盖关键不变量（正反成对）：
  - SQLiteSkillStore：注册去重、单指针版本切换、重复建版拒绝、回滚、
    四计数器三分支打点事务性、metrics 零除安全、top/health 边界、eval 持久化往返
  - SkillSelector：store 缺省、override 强制与 disabled 过滤、quality filter 压线边界、
    LLM 选择有效 id 过滤、LLM 失败降级 effective_rate 排序
  - parser：frontmatter 四类畸形输入全部 ValueError；sidecar 幂等；BUILTIN 确定性 id
  - checks：纯函数逐项正反
"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from novamind.core.skill.eval.analyzers import checks
from novamind.core.skill.parser import parse_skill_file, read_or_create_skill_id
from novamind.core.skill.selector import SkillSelector
from novamind.core.skill.store import SQLiteSkillStore
from novamind.core.skill.types import SkillLineage, SkillRecord


def _rec(name="demo", skill_id=None, gen=0, enabled=True,
         sel=0, app=0, comp=0, fb=0) -> SkillRecord:
    return SkillRecord(
        skill_id=skill_id or f"{name}__{gen}", name=name, path=f"/tmp/{name}.md",
        content_hash="h1", description="d", enabled=enabled,
        lineage=SkillLineage(origin="IMPORTED", generation=gen),
        total_selections=sel, total_applied=app,
        total_completions=comp, total_fallbacks=fb,
    )


class TestSQLiteSkillStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteSkillStore(Path(self.tmp.name) / "skills.db")
        self.addCleanup(self.store.close)

    def test_register_get_roundtrip_preserves_lineage(self):
        rec = replace(_rec(), lineage=SkillLineage(parent_skill_ids=("p1", "p2"),
                                                   generation=2, origin="FIXED"))
        self.store.register(rec)
        got = self.store.get(rec.skill_id)
        self.assertEqual(got.lineage.parent_skill_ids, ("p1", "p2"))
        self.assertEqual(got.lineage.generation, 2)

    def test_register_duplicate_id_is_noop(self):
        self.store.register(_rec(skill_id="dup"))
        self.store.register(replace(_rec(skill_id="dup"), content_hash="changed"))
        rows = [r for r in self.store.list_active() if r.skill_id == "dup"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content_hash, "h1")  # 未被覆盖

    def test_set_enabled_missing_returns_false(self):
        self.assertFalse(self.store.set_enabled("ghost", False))
        self.store.register(_rec())
        self.assertTrue(self.store.set_enabled(_rec().skill_id, False))
        self.assertFalse(self.store.get(_rec().skill_id).enabled)

    def test_create_version_single_pointer_and_dup_rejected(self):
        base = _rec(name="a", skill_id="a__v0")
        self.store.register(base)
        v1 = replace(_rec(name="a", skill_id="a__v1", gen=1),
                     lineage=SkillLineage(origin="FIXED", generation=1))
        self.store.create_version("a__v0", v1, "FIXED")
        self.assertIsNone(self.store.get_active("a") or None) if False else None
        active = self.store.get_active("a")
        self.assertEqual(active.skill_id, "a__v1")
        with self.assertRaises(ValueError):
            self.store.create_version("a__v1", v1, "FIXED")

    def test_rollback_switches_pointer_and_missing_is_noop(self):
        v0 = _rec(name="b", skill_id="b__v0")
        self.store.register(v0)
        v1 = replace(_rec(name="b", skill_id="b__v1", gen=1),
                     lineage=SkillLineage(origin="FIXED", generation=1))
        self.store.create_version("b__v0", v1, "FIXED")
        self.store.rollback("b__v0")
        self.assertEqual(self.store.get_active("b").skill_id, "b__v0")
        versions = self.store.get_versions("b")
        self.assertEqual([v.skill_id for v in versions], ["b__v0", "b__v1"])
        self.store.rollback("ghost")  # 不存在 → 静默

    def test_record_outcome_three_branches(self):
        rid = _rec(skill_id="m").skill_id
        self.store.register(_rec(skill_id=rid))
        self.store.record_outcome(rid, "r1", applied=True, task_completed=True)
        m = self.store.get_metrics(rid)
        self.assertEqual((m.applied, m.completions, m.fallbacks), (1, 1, 0))
        self.store.record_outcome(rid, "r2", applied=False, task_completed=False)
        m = self.store.get_metrics(rid)
        self.assertEqual(m.fallbacks, 1)
        self.store.record_outcome(rid, "r3", applied=None, task_completed=False)
        m = self.store.get_metrics(rid)
        self.assertEqual((m.applied, m.completions, m.fallbacks), (1, 1, 1))
        # 未知 skill 直接返回，不写 judgment 也不崩
        self.store.record_outcome("ghost", "r4", applied=True, task_completed=True)

    def test_metrics_zero_division_safe(self):
        rec = _rec(skill_id="z")
        self.store.register(rec)
        m = self.store.get_metrics(rec.skill_id)
        self.assertEqual((m.applied_rate, m.effective_rate, m.fallback_rate), (0.0, 0.0, 0.0))
        self.assertIsNone(self.store.get_metrics("ghost"))

    def _seed_counters(self, skill_id: str, *, selections: int, completions: int,
                       fallbacks: int = 0) -> None:
        """register 只建身份不落计数器（计数器仅经打点累积），这里直接置数造场景。"""
        self.store._conn.execute(
            "UPDATE skill_records SET total_selections=?, total_completions=?, "
            "total_fallbacks=?, total_applied=? WHERE skill_id=?",
            (selections, completions, fallbacks, completions, skill_id))
        self.store._conn.commit()

    def test_top_skills_filters_min_selections_and_sorts(self):
        for name, comp in (("low", 1), ("high", 9), ("mid", 3)):
            rid = f"{name}__x"
            self.store.register(_rec(name=name, skill_id=rid))
            self._seed_counters(rid, selections=10, completions=comp)
        self.store.register(_rec(name="few2", skill_id="few2__x"))  # 选择数不足
        self._seed_counters("few2__x", selections=3, completions=3)
        top = self.store.get_top_skills(2)
        self.assertEqual([r.name for r in top], ["high", "mid"])  # few*/low 被 min_selections 滤掉

    def test_health_check_degraded_boundary(self):
        cases = {"ok": (10, 5), "bad": (10, 3), "fresh": (2, 0)}  # 有效率 50%/30%/0%
        for name, (sel, comp) in cases.items():
            rid = f"{name}__h"
            self.store.register(_rec(name=name, skill_id=rid))
            self._seed_counters(rid, selections=sel, completions=comp)
        health = {h.name: h.degraded for h in self.store.health_check()}
        self.assertFalse(health["ok"])
        self.assertTrue(health["bad"])      # 30% < 40% 且选择数足 → 退化
        self.assertFalse(health["fresh"])   # 选择数不足 → 视为新技能不判退化

    def test_eval_persistence_roundtrips(self):
        from types import SimpleNamespace
        judgment = SimpleNamespace(judgment_id="j1", skill_id="s", skill_name="n",
                                   task_id="t", skill_applied=True,
                                   deviation_note="", timestamp="")
        self.store.save_judgment(judgment)
        got = self.store.get_judgments("s")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].skill_applied)

        score = SimpleNamespace(score_id="sc1", task_id="t1", task_completion=0.9,
                                response_quality=0.8, efficiency=0.7, tool_usage=0.8,
                                overall_score=0.85, rationale="r", timestamp="")
        self.store.save_task_score(score)
        got_score = self.store.get_task_scores("t1")
        self.assertAlmostEqual(got_score.overall_score, 0.85)
        self.assertIsNone(self.store.get_task_scores("nope"))

        run = SimpleNamespace(eval_run_id="run1", eval_layer="L1", skill_ids=["s"],
                              candidate_id="c", baseline_id="b",
                              result_json="{}", timestamp="")
        self.assertEqual(self.store.save_eval_run(run), "run1")

    def test_evolution_history_roundtrip(self):
        from types import SimpleNamespace
        rec = SimpleNamespace(evolution_id="e1", skill_name="demo",
                              evolution_type="FIX", trigger="METRIC",
                              baseline_id="b", candidate_id="c",
                              failure_focus="f", mutation_diff="d",
                              eval_score=0.9, gate_decision="accept",
                              created_version_id="v", timestamp="")
        self.store.record_evolution(rec)
        history = self.store.get_evolution_history("demo")
        self.assertEqual(history[0]["evolution_id"], "e1")

    def test_discover_upserts_from_filesystem(self):
        root = Path(self.tmp.name) / "skills" / "alpha"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: 第一个\n---\nbody\n", encoding="utf-8")
        found = self.store.discover([Path(self.tmp.name) / "skills"], origin="IMPORTED")
        self.assertEqual(len(found), 1)
        first_hash = found[0].content_hash
        # 内容变更后再发现 → 更新而非新增（_upsert update 分支）
        (root / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: 改过\n---\nbody2\n", encoding="utf-8")
        again = self.store.discover([Path(self.tmp.name) / "skills"])
        self.assertEqual(len(self.store.list_active()), 1)
        self.assertNotEqual(again[0].content_hash, first_hash)


class _StoreStub:
    def __init__(self, records):
        self._records = {r.name: r for r in records}

    def get_active(self, name):
        return self._records.get(name)

    def list_active(self):
        return list(self._records.values())


class TestSkillSelector(unittest.TestCase):
    def _llm(self, reply):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.invoke.return_value = type("R", (), {"content": reply})()
        return m

    @staticmethod
    def _skill(name, sel=10, comp=8, enabled=True):
        return _rec(name=name, skill_id=name, sel=sel, comp=comp, enabled=enabled)

    def test_store_none_returns_empty(self):
        self.assertEqual(SkillSelector(store=None).select_for_task("t"), [])

    def test_override_forced_but_disabled_skipped(self):
        disabled = self._skill("off", enabled=False)
        live = self._skill("live")
        selector = SkillSelector(store=_StoreStub([disabled, live]))
        picked = selector.select_for_task("t", overrides=["off", "live"])
        self.assertEqual([p.name for p in picked], ["live"])

    def test_quality_filter_boundary_keeps_exactly_threshold(self):
        """压线边界：effective_rate == threshold 保留（条件是严格小于才滤掉）。"""
        at_line = self._skill("at", sel=10, comp=3)   # 30% == threshold
        under = self._skill("under", sel=10, comp=2)  # 20% < threshold → 滤掉
        selector = SkillSelector(store=_StoreStub([at_line, under]),
                                 quality_threshold=0.3, max_skills=10)
        picked = {p.name for p in selector.select_for_task("t")}
        self.assertIn("at", picked)
        self.assertNotIn("under", picked)

    def test_under_max_skills_no_llm_needed(self):
        a, b = self._skill("a"), self._skill("b")
        selector = SkillSelector(store=_StoreStub([a, b]), max_skills=3)
        self.assertEqual(len(selector.select_for_task("t")), 2)

    def test_llm_selection_filters_unknown_ids(self):
        many = [self._skill(f"s{i}") for i in range(5)]
        selector = SkillSelector(store=_StoreStub(many), max_skills=2,
                                 llm=self._llm('{"skills": ["s3", "ghost"]}'))
        picked = selector.select_for_task("t")
        self.assertEqual([p.name for p in picked], ["s3"])

    def test_llm_failure_falls_back_to_effective_rate_ranking(self):
        many = [(self._skill(f"s{i}", comp=7 + i)) for i in range(5)]  # 全部高于阈值，s4 最高
        from unittest.mock import MagicMock
        llm = MagicMock()
        llm.invoke.side_effect = RuntimeError("api down")
        selector = SkillSelector(store=_StoreStub(many), max_skills=2, llm=llm)
        picked = selector.select_for_task("t")
        self.assertEqual([p.name for p in picked], ["s4", "s3"])

    def test_empty_llm_selection_falls_back_to_ranking(self):
        many = [self._skill(f"s{i}", comp=7 + i) for i in range(4)]
        selector = SkillSelector(store=_StoreStub(many), max_skills=2,
                                 llm=self._llm('{"skills": []}'))
        picked = selector.select_for_task("t")
        self.assertEqual([p.name for p in picked], ["s3", "s2"])


_SKILL_MD_OK = "---\nname: alpha\ndescription: 描述\nallowed-tools:\n  - read_a\n  - write_a\n---\n正文\n"


class TestParser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, content: str, filename="SKILL.md") -> Path:
        f = self.dir / filename
        f.write_text(content, encoding="utf-8")
        return f

    def test_valid_parse_with_tools_and_enabled_flag(self):
        rec = parse_skill_file(self._write(
            "---\nname: a\ndescription: d\nallowed-tools: [x, y]\nenabled: false\n---\nbody"))
        self.assertEqual(rec.allowed_tools, ("x", "y"))
        self.assertFalse(rec.enabled)
        self.assertEqual(len(rec.content_hash), 16)

    def test_builtin_deterministic_id(self):
        rec = parse_skill_file(self._write(_SKILL_MD_OK.replace("alpha", "core-x")),
                               origin="BUILTIN")
        self.assertEqual(rec.skill_id, "core-x__builtin")

    def test_sidecar_id_reused_across_parses(self):
        f = self._write(_SKILL_MD_OK)
        id1 = parse_skill_file(f).skill_id
        id2 = parse_skill_file(f).skill_id
        self.assertEqual(id1, id2)
        self.assertTrue(read_or_create_skill_id(self.dir, "alpha"))

    def test_missing_frontmatter_raises(self):
        with self.assertRaises(ValueError):
            parse_skill_file(self._write("no frontmatter here"))

    def test_bad_yaml_raises_valueerror(self):
        with self.assertRaises(ValueError):
            parse_skill_file(self._write("---\n: : :\n---\nbody"))

    def test_non_mapping_frontmatter_raises(self):
        with self.assertRaises(ValueError):
            parse_skill_file(self._write("---\n- just\n- a list\n---\nbody"))

    def test_missing_name_or_description_raises(self):
        with self.assertRaises(ValueError):
            parse_skill_file(self._write("---\ndescription: d\n---\nb"))
        with self.assertRaises(ValueError):
            parse_skill_file(self._write("---\nname: n\n---\nb"))


class TestChecks(unittest.TestCase):
    def test_read_content_missing_file_returns_empty(self):
        class R:
            path = "/nonexistent/x.md"
        self.assertEqual(checks.read_content(R()), "")

    def test_split_body_with_and_without_frontmatter(self):
        self.assertEqual(checks.split_body("---\na: b\n---\nBODY"), "BODY")
        self.assertEqual(checks.split_body("PLAIN"), "PLAIN")

    def test_check_nonempty(self):
        self.assertTrue(checks.check_nonempty("---\nx: y\n---\ntext"))
        self.assertFalse(checks.check_nonempty("---\nx: y\n---\n   \n"))
        self.assertFalse(checks.check_nonempty(""))

    def test_check_json_parseable_yaml(self):
        self.assertTrue(checks.check_json_parseable("no fm"))
        self.assertTrue(checks.check_json_parseable("---\nname: x\n---\nb"))
        self.assertFalse(checks.check_json_parseable("---\n: :\n---\nb"))

    def test_must_cite_patterns(self):
        for text in ("见 https://example.com", "作者 @someone 说", "来源：官方文档"):
            self.assertTrue(checks.check_must_cite(text))
        self.assertFalse(checks.check_must_cite("没有任何出处说明"))

    def test_paragraph_limit_boundary(self):
        body_ok = "\n\n".join(f"p{i}" for i in range(20))
        body_over = "\n\n".join(f"p{i}" for i in range(21))
        self.assertTrue(checks.check_paragraph_limit(body_ok))
        self.assertFalse(checks.check_paragraph_limit(body_over))

    def test_lead_with_conclusion_first_three_paras_only(self):
        self.assertTrue(checks.check_lead_with_conclusion("结论如下\n\n细节"))
        late = "\n\n".join(["a", "b", "c", "结论在第四段"])
        self.assertFalse(checks.check_lead_with_conclusion(late))
        self.assertFalse(checks.check_lead_with_conclusion(""))

    def test_unfounded_claims_chinese_and_english(self):
        self.assertFalse(checks.check_no_unfounded_claims("这绝对是最好的"))
        self.assertFalse(checks.check_no_unfounded_claims("definitely works"))
        self.assertTrue(checks.check_no_unfounded_claims("通常效果不错"))

    def test_semantic_density_directive_ratio(self):
        self.assertEqual(checks.semantic_density(""), 0.0)
        density = checks.semantic_density("You MUST do it. ALWAYS check.")
        self.assertGreater(density, 0.0)


if __name__ == "__main__":
    unittest.main()
