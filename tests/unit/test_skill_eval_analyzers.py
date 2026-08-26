"""
技能注入 + eval analyzers 边界测试（P1 覆盖率盲区补齐，LLM 全部 mock）。

覆盖关键不变量：
  - injector：空列表返空串、frontmatter 剥离、文件缺失容错
  - TaskQualityJudge：无 LLM 返 None、四维 clamp 到 [0,1]、加权总分公式、
    畸形 JSON 回退 None、store 保存异常不影响结果
  - SkillJudgmentAnalyzer：applied/deviation 解析、畸形 JSON 回退、保存容错
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from novamind.core.skill.eval.analyzers.skill_judgment_analyzer import (
    SkillJudgmentAnalyzer,
)
from novamind.core.skill.eval.analyzers.task_quality_judge import TaskQualityJudge
from novamind.core.skill.injector import build_injection_text
from novamind.core.skill.types import SkillLineage, SkillRecord


class _Resp:
    def __init__(self, content):
        self.content = content


def _llm(reply: str) -> MagicMock:
    m = MagicMock()
    m.invoke.return_value = _Resp(reply)
    return m


def _record(name="demo", path="/tmp/nonexistent.md") -> SkillRecord:
    return SkillRecord(skill_id="s1", name=name, path=path, content_hash="h",
                       lineage=SkillLineage())


class TestInjector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_empty_skills_returns_empty_string(self):
        self.assertEqual(build_injection_text([]), "")

    def test_injection_includes_name_path_and_body(self):
        p = self.root / "a.md"
        p.write_text("# Steps\n1. do it\n", encoding="utf-8")
        text = build_injection_text([_record(path=str(p))])
        self.assertIn("# Active Skills", text)
        self.assertIn("### Skill: demo", text)
        self.assertIn("do it", text)

    def test_frontmatter_stripped_from_body(self):
        p = self.root / "b.md"
        p.write_text("---\nname: x\ndescription: y\n---\nreal body\n", encoding="utf-8")
        text = build_injection_text([_record(path=str(p))])
        self.assertIn("real body", text)
        self.assertNotIn("description: y", text)

    def test_missing_file_tolerated_empty_body(self):
        text = build_injection_text([_record(path=str(self.root / "ghost.md"))])
        self.assertIn("### Skill: demo", text)


class TestTaskQualityJudge(unittest.TestCase):
    def test_no_llm_returns_none(self):
        judge = TaskQualityJudge(llm=None)
        self.assertIsNone(asyncio.run(judge.judge_task("t1", "trace", "out")))

    def test_scores_clamped_and_weighted(self):
        """越界分数被钳制到 [0,1]，overall 按权重 0.50/0.35/0.05/0.10 加权。"""
        llm = _llm('{"task_completion": 1.5, "response_quality": -0.2, '
                   '"efficiency": 0.5, "tool_usage": 0.8, "rationale": "ok"}')
        judge = TaskQualityJudge(llm=llm)
        score = asyncio.run(judge.judge_task("t1", "trace", "out"))
        self.assertIsNotNone(score)
        self.assertEqual(score.task_completion, 1.0)   # 钳到上限
        self.assertEqual(score.response_quality, 0.0)  # 钳到下限
        expected = round(1.0 * 0.50 + 0.0 * 0.35 + 0.5 * 0.05 + 0.8 * 0.10, 3)
        self.assertAlmostEqual(score.overall_score, expected, places=3)

    def test_malformed_json_returns_none(self):
        judge = TaskQualityJudge(llm=_llm("no json here"))
        self.assertIsNone(asyncio.run(judge.judge_task("t1", "t", "o")))

    def test_llm_raise_returns_none(self):
        llm = MagicMock()
        llm.invoke.side_effect = RuntimeError("api down")
        judge = TaskQualityJudge(llm=llm)
        self.assertIsNone(asyncio.run(judge.judge_task("t1", "t", "o")))

    def test_store_save_failure_does_not_affect_score(self):
        store = MagicMock()
        store.save_task_score.side_effect = RuntimeError("db locked")
        llm = _llm('{"task_completion": 0.9, "response_quality": 0.8, '
                   '"efficiency": 0.7, "tool_usage": 0.8}')
        judge = TaskQualityJudge(llm=llm, store=store)
        score = asyncio.run(judge.judge_task("t1", "t", "o"))
        self.assertIsNotNone(score)
        self.assertTrue(store.save_task_score.called)


class TestSkillJudgmentAnalyzer(unittest.TestCase):
    def test_no_llm_returns_none(self):
        analyzer = SkillJudgmentAnalyzer(llm=None)
        self.assertIsNone(asyncio.run(analyzer.judge("s1", "demo", "t1", "trace")))

    def test_applied_true_parses(self):
        analyzer = SkillJudgmentAnalyzer(llm=_llm('{"applied": true, "deviation": ""}'))
        j = asyncio.run(analyzer.judge("s1", "demo", "t1", "trace"))
        self.assertTrue(j.skill_applied)
        self.assertEqual(j.skill_id, "s1")

    def test_deviation_captured_when_not_applied(self):
        analyzer = SkillJudgmentAnalyzer(
            llm=_llm('{"applied": false, "deviation": "跳过了第2步"}'))
        j = asyncio.run(analyzer.judge("s1", "demo", "t1", "trace"))
        self.assertFalse(j.skill_applied)
        self.assertIn("跳过", j.deviation_note)

    def test_malformed_json_returns_none_but_store_not_called(self):
        store = MagicMock()
        analyzer = SkillJudgmentAnalyzer(llm=_llm("garbage"), store=store)
        result = asyncio.run(analyzer.judge("s1", "demo", "t1", "trace"))
        self.assertIsNone(result)
        self.assertFalse(store.save_judgment.called)

    def test_store_failure_swallowed(self):
        store = MagicMock()
        store.save_judgment.side_effect = RuntimeError("x")
        analyzer = SkillJudgmentAnalyzer(
            llm=_llm('{"applied": true}'), store=store)
        j = asyncio.run(analyzer.judge("s1", "demo", "t1", "trace"))
        self.assertTrue(j.skill_applied)


if __name__ == "__main__":
    unittest.main()
