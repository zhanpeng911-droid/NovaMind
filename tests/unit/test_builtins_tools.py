"""
内置工具面测试（P1 覆盖率盲区补齐）。

覆盖关键不变量（正反成对）：
  - calculator/safe_calculate：四则/幂/负数正常；AST 安全（调用、属性、变量名拒绝）；
    除零/语法错误/不安全运算符的友好错误
  - get_system_model_info：env 齐全 / 缺失
  - save_user_profile：原子覆写 + 时间戳备份 + 超 10 份清理
  - 定时任务：格式错误/过去时间拒绝；增删改查正常路径与未找到路径；
    new_time 过去时间拒绝；文件缺失分支
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from novamind.core.tools import builtins
from novamind.core.tools.builtins import (
    calculator,
    delete_scheduled_task,
    get_current_time,
    get_system_model_info,
    list_scheduled_tasks,
    modify_scheduled_task,
    safe_calculate,
    save_user_profile,
    schedule_task,
)


class TestCalculator(unittest.TestCase):
    def test_basic_arithmetic(self):
        self.assertEqual(safe_calculate("3 * (5 + 2)"), 21.0)
        self.assertEqual(safe_calculate("2 ** 10"), 1024.0)
        self.assertEqual(safe_calculate("-5 + 3"), -2.0)
        self.assertEqual(safe_calculate("7 // 2"), 3.0)
        self.assertEqual(safe_calculate("7 % 3"), 1.0)

    def test_rejects_function_call(self):
        with self.assertRaises(ValueError):
            safe_calculate("__import__('os').system('ls')")

    def test_rejects_attribute_access(self):
        with self.assertRaises(ValueError):
            safe_calculate("(1).real")

    def test_rejects_variable_name(self):
        with self.assertRaises(ValueError):
            safe_calculate("x + 1")

    def test_rejects_unsafe_operator(self):
        # << 位运算是 BinOp 但不在白名单
        with self.assertRaises(ValueError):
            safe_calculate("1 << 2")

    def test_calculator_tool_success_integer_format(self):
        out = calculator.invoke({"expression": "6 / 2"})
        self.assertIn("3", out)
        self.assertIn("计算结果", out)

    def test_calculator_division_by_zero(self):
        self.assertIn("除数不能为零", calculator.invoke({"expression": "1 / 0"}))

    def test_calculator_syntax_error(self):
        self.assertIn("语法不正确", calculator.invoke({"expression": "3 * +"}))

    def test_calculator_unsafe_expression(self):
        out = calculator.invoke({"expression": "__import__('os')"})
        self.assertTrue(out.startswith("计算出错"))


class TestSystemModelInfo(unittest.TestCase):
    def test_returns_provider_and_model_when_configured(self):
        with patch.dict(os.environ, {"DEFAULT_PROVIDER": "deepseek",
                                     "DEFAULT_MODEL": "v4-flash"}):
            out = get_system_model_info.invoke({})
        self.assertIn("deepseek", out)
        self.assertIn("v4-flash", out)

    def test_reports_unknown_when_env_missing(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DEFAULT_PROVIDER", "DEFAULT_MODEL")}
        with patch.dict(os.environ, env, clear=True):
            out = get_system_model_info.invoke({})
        self.assertIn("无法获取", out)


class TestSaveUserProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name) / "memory"
        self.backup = self.memory / "backups"
        self.profile = self.memory / "user_profile.md"
        patchers = [
            patch.object(builtins, "MEMORY_DIR", str(self.memory)),
            patch("novamind.core.config.PROFILE_PATH", str(self.profile)),
            patch.object(builtins, "PROFILE_BACKUP_DIR", str(self.backup)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _backup_count(self):
        return len(list(self.backup.glob("user_profile.*.md"))) if self.backup.exists() else 0

    def test_first_save_creates_file_without_backup(self):
        save_user_profile.invoke({"new_content": "# v1"})
        self.assertEqual(self.profile.read_text(encoding="utf-8"), "# v1")
        self.assertEqual(self._backup_count(), 0)

    def test_overwrite_creates_timestamped_backup(self):
        save_user_profile.invoke({"new_content": "# v1"})
        save_user_profile.invoke({"new_content": "# v2"})
        self.assertEqual(self.profile.read_text(encoding="utf-8"), "# v2")
        self.assertEqual(self._backup_count(), 1)
        backup_text = next(self.backup.glob("user_profile.*.md")).read_text(encoding="utf-8")
        self.assertEqual(backup_text, "# v1")

    def test_backups_pruned_to_max_ten(self):
        """备份名时间戳精确到秒：用假时钟推进，验证只保留最近 10 份。"""
        from datetime import timedelta

        class AdvancingDateTime(builtins.datetime):
            offset_seconds = 0

            @classmethod
            def now(cls):
                return super().now() + timedelta(seconds=cls.offset_seconds)

        with patch.object(builtins, "datetime", AdvancingDateTime):
            for i in range(13):
                AdvancingDateTime.offset_seconds = i * 60
                save_user_profile.invoke({"new_content": f"# v{i}"})

        self.assertEqual(self._backup_count(), 10)
        # 最老的被清掉，最新保留
        backups = sorted(p.read_text(encoding="utf-8") for p in self.backup.glob("user_profile.*.md"))
        self.assertNotIn("# v0", backups)
        self.assertIn("# v12", self.profile.read_text(encoding="utf-8"))


class TestScheduledTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks_file = Path(self.tmp.name) / "tasks.json"
        # TASKS_FILE 已收敛为 task_store 单一来源，只 patch 一处
        self._patch = patch("novamind.core.task_store.TASKS_FILE", str(self.tasks_file))
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    def _seed(self, n=1):
        # 直接写补丁后的 tasks 文件（task_store 的 TASKS_FILE 是独立绑定，不走 patch）
        import json
        tasks = [{"id": f"id{i}", "target_time": self.future,
                  "description": f"task{i}", "repeat": None, "repeat_count": None}
                 for i in range(n)]
        self.tasks_file.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
        return tasks

    def test_schedule_bad_format_rejected(self):
        self.assertIn("时间格式错误", schedule_task.invoke({"target_time": "tomorrow 8am", "description": "hi"}))

    def test_schedule_past_time_rejected(self):
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertIn("必须晚于当前时间", schedule_task.invoke({"target_time": past, "description": "hi"}))

    def test_schedule_success_single_and_repeat_message(self):
        out = schedule_task.invoke({"target_time": self.future, "description": "喝水"})
        self.assertIn("任务已成功加入队列", out)
        out2 = schedule_task.invoke({"target_time": self.future, "description": "吃药", "repeat": "daily", "repeat_count": 3})
        self.assertIn("daily", out2)
        self.assertIn("3 次", out2)

    def test_list_empty_when_no_file(self):
        self.assertIn("没有任何定时任务", list_scheduled_tasks.invoke({}))

    def test_list_sorted_output(self):
        self._seed(2)
        out = list_scheduled_tasks.invoke({})
        self.assertIn("task0", out)
        self.assertIn("[ID: id0]", out)

    def test_delete_missing_file(self):
        self.assertIn("文件不存在", delete_scheduled_task.invoke({"task_id": "id0"}))

    def test_delete_not_found_and_success(self):
        self._seed(1)
        self.assertIn("未找到 ID", delete_scheduled_task.invoke({"task_id": "ghost"}))
        self.assertIn("已成功取消", delete_scheduled_task.invoke({"task_id": "id0"}))
        self.assertIn("没有任何定时任务", list_scheduled_tasks.invoke({}))

    def test_modify_time_and_description(self):
        self._seed(1)
        new_time = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        out = modify_scheduled_task.invoke({"task_id": "id0", "new_time": new_time, "new_description": "改名了"})
        self.assertIn("已成功更新", out)
        listing = list_scheduled_tasks.invoke({})
        self.assertIn("改名了", listing)

    def test_modify_past_new_time_rejected(self):
        self._seed(1)
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertIn("new_time 必须晚于当前时间", modify_scheduled_task.invoke({"task_id": "id0", "new_time": past}))

    def test_modify_bad_time_format(self):
        self._seed(1)
        self.assertIn("时间格式错误", modify_scheduled_task.invoke({"task_id": "id0", "new_time": "not-a-time"}))

    def test_modify_missing_file_and_not_found(self):
        self.assertIn("任务列表为空", modify_scheduled_task.invoke({"task_id": "id0"}))
        self._seed(1)
        self.assertIn("未找到 ID", modify_scheduled_task.invoke({"task_id": "ghost", "new_description": "x"}))


class TestGetCurrentTime(unittest.TestCase):
    def test_returns_formatted_time(self):
        out = get_current_time.invoke({})
        self.assertIn("当前本地系统时间", out)


if __name__ == "__main__":
    unittest.main()


class TestBackupMillisecondResolution(unittest.TestCase):
    """P3-2 回归：备份文件名含毫秒，同秒内连续保存生成两个备份。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name) / "memory"
        self.backup = self.memory / "backups"
        self.profile = self.memory / "user_profile.md"
        patchers = [
            patch.object(builtins, "MEMORY_DIR", str(self.memory)),
            patch("novamind.core.config.PROFILE_PATH", str(self.profile)),
            patch.object(builtins, "PROFILE_BACKUP_DIR", str(self.backup)),
        ]
        for p_ in patchers:
            p_.start()
            self.addCleanup(p_.stop)

    def test_same_second_two_saves_two_backups(self):
        """冻结时钟在同一秒，两次保存仍产生两个独立备份（毫秒位不同）。"""
        from datetime import datetime

        class Advancing(datetime):
            counter = 0

            @classmethod
            def now(cls):
                cls.counter += 1
                # 同一秒内两次调用微秒不同（真实时钟天然如此，这里显式模拟）
                return datetime(2026, 8, 26, 12, 0, 0, cls.counter * 1000)

        with patch.object(builtins, "datetime", Advancing):
            save_user_profile.invoke({"new_content": "# v1"})  # 首次保存无备份
            save_user_profile.invoke({"new_content": "# v2"})  # 产生第 1 个备份
            save_user_profile.invoke({"new_content": "# v3"})  # 产生第 2 个备份
        backups = list(self.backup.glob("user_profile.*.md"))
        self.assertEqual(len(backups), 2)
        # 同秒内两次保存的备份文件名不同（毫秒位区分）
        names = [b.name for b in backups]
        self.assertEqual(len(set(names)), 2)
