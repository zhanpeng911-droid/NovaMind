"""RuntimeTracker 趋势层测试。"""

import unittest
from unittest import mock

from novamind.core.skill.eval.runtime_tracker import RuntimeTracker
from novamind.core.skill.eval.types import SkillJudgment


def _j(applied: bool) -> SkillJudgment:
    return SkillJudgment(
        judgment_id="j", skill_id="s1", skill_name="n", task_id="t",
        skill_applied=applied,
    )


class TestComputeTrend(unittest.TestCase):
    def test_insufficient_data(self):
        self.assertEqual(RuntimeTracker._compute_trend([], 0.15), "insufficient_data")
        self.assertEqual(
            RuntimeTracker._compute_trend([_j(True), _j(True), _j(False)], 0.15),
            "insufficient_data",
        )

    def test_improving(self):
        # recent 全 applied，older 全 not → diff=1
        judgments = [_j(True), _j(True), _j(False), _j(False)]
        self.assertEqual(RuntimeTracker._compute_trend(judgments, 0.15), "improving")

    def test_degrading(self):
        judgments = [_j(False), _j(False), _j(True), _j(True)]
        self.assertEqual(RuntimeTracker._compute_trend(judgments, 0.15), "degrading")

    def test_stable(self):
        judgments = [_j(True), _j(False), _j(True), _j(False)]
        self.assertEqual(RuntimeTracker._compute_trend(judgments, 0.15), "stable")


class TestHealthReport(unittest.TestCase):
    def test_no_metrics_returns_insufficient(self):
        store = mock.MagicMock()
        store.get_metrics.return_value = None
        store.get_judgments.return_value = []
        tracker = RuntimeTracker(store)
        report = tracker.health_report("s1")
        self.assertEqual(report.trend, "insufficient_data")
        self.assertEqual(report.window_selections, 0)

    def test_report_uses_metrics(self):
        metrics = mock.MagicMock()
        metrics.selections = 10
        metrics.applied = 8
        metrics.applied_rate = 0.8
        metrics.completion_rate = 0.7
        metrics.effective_rate = 0.6
        metrics.fallback_rate = 0.1
        store = mock.MagicMock()
        store.get_metrics.return_value = metrics
        store.get_judgments.return_value = [_j(True), _j(True), _j(False), _j(False)]
        store.get.return_value = mock.MagicMock(name="n")
        tracker = RuntimeTracker(store)
        report = tracker.health_report("s1")
        self.assertEqual(report.window_selections, 10)
        self.assertEqual(report.effective_rate, 0.6)
        self.assertEqual(report.trend, "improving")


class TestDegradedSkills(unittest.TestCase):
    def test_returns_degrading_skill_ids(self):
        skill = mock.MagicMock()
        skill.skill_id = "s1"
        store = mock.MagicMock()
        store.list_active.return_value = [skill]
        # 4 条：recent 全 not applied → degrading
        store.get_judgments.return_value = [_j(False), _j(False), _j(True), _j(True)]
        tracker = RuntimeTracker(store)
        self.assertEqual(tracker.degraded_skills(), ["s1"])

    def test_no_degradation_when_stable(self):
        skill = mock.MagicMock()
        skill.skill_id = "s1"
        store = mock.MagicMock()
        store.list_active.return_value = [skill]
        store.get_judgments.return_value = [_j(True), _j(False), _j(True), _j(False)]
        tracker = RuntimeTracker(store)
        self.assertEqual(tracker.degraded_skills(), [])


if __name__ == "__main__":
    unittest.main()
