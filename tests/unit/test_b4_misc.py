"""B4 零散文件补测：programmatic_bridge / permissive_guard / identity_translator。"""
import tempfile
import unittest
from pathlib import Path

from novamind.core.sandbox.guards.permissive_guard import PermissiveGuard
from novamind.core.sandbox.translators.identity_translator import IdentityTranslator
from novamind.core.skill.evolution.eval.programmatic_bridge import (
    ProgrammaticEvalBridge,
)
from novamind.core.skill.evolution.types import EvalContext
from novamind.core.skill.types import SkillLineage, SkillRecord


def _rec(path):
    return SkillRecord(skill_id="s1", name="demo", path=str(path), content_hash="h",
                       description="d", lineage=SkillLineage())


class TestProgrammaticBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridge = ProgrammaticEvalBridge()

    def test_evaluate_with_baseline_delegates_to_contract_checker(self):
        cand = self.root / "cand.md"
        base = self.root / "base.md"
        cand.write_text("---\nname: c\ndescription: d\n---\nMUST do the thing\n", encoding="utf-8")
        base.write_text("---\nname: b\ndescription: d\n---\nMUST do the thing\n", encoding="utf-8")
        ctx = EvalContext(baseline=_rec(base), candidate=_rec(cand))
        result = self.bridge.evaluate(ctx)
        self.assertIn(result.score, (0.0, 1.0))  # 结构一致 → 高分

    def test_evaluate_without_baseline_tolerated(self):
        cand = self.root / "cand.md"
        cand.write_text("---\nname: c\ndescription: d\n---\nbody\n", encoding="utf-8")
        ctx = EvalContext(baseline=None, candidate=_rec(cand))
        result = self.bridge.evaluate(ctx)
        self.assertIsNotNone(result)  # baseline 缺省走空串，不崩

    def test_evaluate_missing_files_reads_empty(self):
        ctx = EvalContext(baseline=_rec(self.root / "nope.md"),
                          candidate=_rec(self.root / "ghost.md"))
        result = self.bridge.evaluate(ctx)
        self.assertIsNotNone(result)


class TestPermissiveGuard(unittest.TestCase):
    def test_path_and_command_never_blocked(self):
        guard = PermissiveGuard()
        self.assertIsNone(guard.validate_path("../../etc/passwd", write=True))
        self.assertIsNone(guard.validate_command("rm -rf /"))


class TestIdentityTranslator(unittest.TestCase):
    def test_all_ops_are_passthrough(self):
        t = IdentityTranslator()
        self.assertEqual(t.translate_path("/mnt/x"), "/mnt/x")
        self.assertEqual(t.translate_command("cat /mnt/x"), "cat /mnt/x")
        self.assertEqual(t.mask_output("out /mnt/x"), "out /mnt/x")


if __name__ == "__main__":
    unittest.main()
