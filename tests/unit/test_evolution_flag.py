"""进化开关（bootstrap 提醒）测试。"""

import os
import unittest

from novamind.core.skill.evolution.flag import (
    MULTIAGENT_EVOLUTION_ENV,
    SKILL_EVOLUTION_ENV,
    evolution_notice,
    multiagent_evolution_enabled,
    skill_evolution_enabled,
)


class TestEvolutionFlag(unittest.TestCase):
    def setUp(self):
        self._saved = {
            SKILL_EVOLUTION_ENV: os.environ.pop(SKILL_EVOLUTION_ENV, None),
            MULTIAGENT_EVOLUTION_ENV: os.environ.pop(MULTIAGENT_EVOLUTION_ENV, None),
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_disabled(self):
        self.assertFalse(skill_evolution_enabled())
        self.assertFalse(multiagent_evolution_enabled())

    def test_truthy_values_enable(self):
        for value in ("1", "on", "true", "yes", "ON", "True"):
            os.environ[SKILL_EVOLUTION_ENV] = value
            self.assertTrue(skill_evolution_enabled(), value)
        os.environ.pop(SKILL_EVOLUTION_ENV)

    def test_falsy_values_stay_disabled(self):
        for value in ("0", "off", "false", "no", "random"):
            os.environ[SKILL_EVOLUTION_ENV] = value
            self.assertFalse(skill_evolution_enabled(), value)
        os.environ.pop(SKILL_EVOLUTION_ENV)

    def test_notice_mentions_env_when_disabled(self):
        notice = evolution_notice()
        self.assertIn(SKILL_EVOLUTION_ENV, notice)
        self.assertIn(MULTIAGENT_EVOLUTION_ENV, notice)

    def test_notice_empty_when_all_enabled(self):
        os.environ[SKILL_EVOLUTION_ENV] = "on"
        os.environ[MULTIAGENT_EVOLUTION_ENV] = "on"
        self.assertEqual(evolution_notice(), "")

    def test_notice_reflects_partial_enable(self):
        os.environ[SKILL_EVOLUTION_ENV] = "on"
        notice = evolution_notice()
        self.assertIn("已开启", notice)
        self.assertIn(MULTIAGENT_EVOLUTION_ENV, notice)


if __name__ == "__main__":
    unittest.main()
