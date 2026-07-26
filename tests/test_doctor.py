"""NovaMind doctor checks tests."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from novamind.core.doctor import run_doctor


class TestDoctor(unittest.TestCase):
    def test_reports_missing_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("novamind.core.doctor.DOCS_DIR", tmpdir), \
                    patch("novamind.core.doctor.POLICY_PATH", os.path.join(tmpdir, "policies.json")), \
                    patch("novamind.core.policy.POLICY_PATH", os.path.join(tmpdir, "policies.json")), \
                    patch("novamind.core.doctor.LOG_DIR", tmpdir):
                report = run_doctor()

            self.assertFalse(report.ok)
            self.assertTrue(any(f.code == "missing_doc" for f in report.findings))
            self.assertTrue(any(f.suggestion for f in report.findings if f.code == "missing_doc"))

    def test_reports_clean_minimal_setup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_dir = os.path.join(tmpdir, "docs")
            harness_dir = os.path.join(tmpdir, "harness")
            logs_dir = os.path.join(tmpdir, "logs")
            os.makedirs(os.path.join(docs_dir, "playbooks"), exist_ok=True)
            os.makedirs(harness_dir, exist_ok=True)
            os.makedirs(logs_dir, exist_ok=True)

            required_docs = {
                "INDEX.md": "index",
                "runtime-overview.md": "runtime",
                "sandbox-policy.md": "sandbox",
                "tool-contracts.md": "Tool usage rules",
                "session-model.md": "session",
                os.path.join("playbooks", "file-edit.md"): "edit",
            }
            for relative_path, content in required_docs.items():
                path = os.path.join(docs_dir, relative_path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)

            policy_path = os.path.join(harness_dir, "policies.json")
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": 1,
                    "default_allowed_tools": [
                        "get_current_time",
                        "calculator",
                        "get_system_model_info",
                        "list_scheduled_tasks",
                        "schedule_task",
                        "delete_scheduled_task",
                        "modify_scheduled_task",
                        "save_user_profile",
                        "list_office_files",
                        "read_office_file",
                        "write_office_file",
                        "execute_office_shell",
                    ],
                    "tool_policies": {},
                }, f)

            with patch("novamind.core.doctor.DOCS_DIR", docs_dir), \
                    patch("novamind.core.doctor.POLICY_PATH", policy_path), \
                    patch("novamind.core.policy.POLICY_PATH", policy_path), \
                    patch("novamind.core.doctor.LOG_DIR", logs_dir):
                report = run_doctor()

            self.assertTrue(report.ok)
            self.assertFalse(any(f.level == "error" for f in report.findings))
            self.assertTrue(hasattr(report, "as_dict"))

    def test_reports_recent_policy_violations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docs_dir = os.path.join(tmpdir, "docs")
            harness_dir = os.path.join(tmpdir, "harness")
            logs_dir = os.path.join(tmpdir, "logs")
            os.makedirs(os.path.join(docs_dir, "playbooks"), exist_ok=True)
            os.makedirs(harness_dir, exist_ok=True)
            os.makedirs(logs_dir, exist_ok=True)

            for relative_path in [
                "INDEX.md",
                "runtime-overview.md",
                "sandbox-policy.md",
                "tool-contracts.md",
                "session-model.md",
                os.path.join("playbooks", "file-edit.md"),
            ]:
                path = os.path.join(docs_dir, relative_path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("Tool usage rules")

            policy_path = os.path.join(harness_dir, "policies.json")
            with open(policy_path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": 1,
                    "default_allowed_tools": [tool for tool in [
                        "get_current_time",
                        "calculator",
                        "get_system_model_info",
                        "list_scheduled_tasks",
                        "schedule_task",
                        "delete_scheduled_task",
                        "modify_scheduled_task",
                        "save_user_profile",
                        "list_office_files",
                        "read_office_file",
                        "write_office_file",
                        "execute_office_shell",
                    ]],
                    "tool_policies": {},
                }, f)

            with open(os.path.join(logs_dir, "test.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"event": "policy_violation", "tool": "write_office_file"}) + "\n")

            with patch("novamind.core.doctor.DOCS_DIR", docs_dir), \
                    patch("novamind.core.doctor.POLICY_PATH", policy_path), \
                    patch("novamind.core.policy.POLICY_PATH", policy_path), \
                    patch("novamind.core.doctor.LOG_DIR", logs_dir):
                report = run_doctor()

            self.assertTrue(any(f.code == "recent_policy_violations" for f in report.findings))

    def test_report_dict_contains_suggestions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("novamind.core.doctor.DOCS_DIR", tmpdir), \
                    patch("novamind.core.doctor.POLICY_PATH", os.path.join(tmpdir, "policies.json")), \
                    patch("novamind.core.policy.POLICY_PATH", os.path.join(tmpdir, "policies.json")), \
                    patch("novamind.core.doctor.LOG_DIR", tmpdir):
                report = run_doctor()

            report_dict = report.as_dict()
            self.assertIn("findings", report_dict)
            self.assertIn("suggestion", report_dict["findings"][0])
