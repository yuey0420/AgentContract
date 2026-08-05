"""P0 regression scenarios: every test name maps to a stable incident class."""

import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pactflow.core.contracts.models import TaskContract
from pactflow.core.contracts.policy import check_read_path, check_shell_command, check_write_path
from pactflow.core.contracts.store import approve_contract, contract_has_valid_approval
from pactflow.core.process.evidence import reconcile_changes
from pactflow.core.process.task_planner import validate_model_plan
from pactflow.core.runtime_store import RuntimeStore


def _contract(**overrides) -> TaskContract:
    data = {
        "contract_version": "2.0",
        "id": "p0-regression",
        "owner": "p0-owner",
        "objective": "protect the workspace",
        "status": "draft",
        "scope": {
            "can_read": ["reports/**"],
            "can_write": ["reports/**"],
            "cannot_write": ["reports/private/**"],
            "can_execute": ["python"],
            "cannot_execute": ["rm -rf", "git push"],
        },
        "tool_policy": {
            "allowed_tools": ["write_office_file", "execute_office_shell"],
            "blocked_tools": ["save_user_profile"],
        },
    }
    data.update(overrides)
    return TaskContract.model_validate(data)


class TestP0PathAndCommandRegression(unittest.TestCase):
    def test_p0_scope_escape_matrix(self):
        contract = _contract()
        cases = [
            "../outside.txt",
            "reports/../outside.txt",
            "reports/private/secret.txt",
            "/etc/passwd",
            "C:\\workspace\\outside.txt",
            "reports-sibling/outside.txt",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertEqual(check_write_path(contract, path).decision, "deny")

    def test_p0_read_escape_matrix(self):
        contract = _contract()
        for path in ["../secret", "/etc/passwd", "C:\\Windows\\win.ini", "reports/../secret"]:
            with self.subTest(path=path):
                self.assertEqual(check_read_path(contract, path).decision, "deny")

    def test_p0_shell_dangerous_variants(self):
        contract = _contract()
        for command in ["rm -rf workspace", "git push origin main", "python script.py"]:
            with self.subTest(command=command):
                decision = check_shell_command(contract, command)
                expected = "allow" if command.startswith("python") else "deny"
                self.assertEqual(decision.decision, expected)


class TestP0ApprovalRegression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(os.path.join(self.temp_dir.name, "runtime.sqlite3"), legacy_tasks_file=None)
        self.run_id = self.store.start_run("p0-thread", "approval", "managed_task")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _action(self, suffix="same"):
        return self.store.create_pending_action(
            self.run_id,
            "p0-thread",
            "execute_office_shell",
            {"command": f"python {suffix}.py"},
            "sha256:contract",
            tool_call_id=f"call-{suffix}",
        )

    def test_p0_approval_binds_exact_args(self):
        action_id = self._action()
        self.assertTrue(self.store.approve_pending_action(action_id, "owner"))
        self.assertIsNone(self.store.consume_action(
            action_id,
            thread_id="p0-thread",
            tool_call_id="call-same",
            tool_name="execute_office_shell",
            args={"command": "python tampered.py"},
            contract_hash="sha256:contract",
        ))

    def test_p0_approval_cannot_be_replayed(self):
        action_id = self._action()
        self.store.approve_pending_action(action_id, "owner")
        kwargs = {
            "thread_id": "p0-thread",
            "tool_call_id": "call-same",
            "tool_name": "execute_office_shell",
            "args": {"command": "python same.py"},
            "contract_hash": "sha256:contract",
        }
        self.assertIsNotNone(self.store.consume_action(action_id, **kwargs))
        self.assertIsNone(self.store.consume_action(action_id, **kwargs))

    def test_p0_expired_and_rejected_approvals_never_execute(self):
        expired = self.store.create_pending_action(
            self.run_id, "p0-thread", "shell", {"command": "python late.py"}, "hash",
            ttl_minutes=-1, tool_call_id="expired",
        )
        self.assertEqual(self.store.get_action(expired)["status"], "expired")
        self.assertFalse(self.store.approve_pending_action(expired, "owner"))

        rejected = self._action("rejected")
        self.assertTrue(self.store.reject_pending_action(rejected, "owner"))
        self.assertIsNone(self.store.consume_action(
            rejected, thread_id="p0-thread", tool_call_id="call-rejected",
            tool_name="execute_office_shell", args={"command": "python rejected.py"},
            contract_hash="sha256:contract",
        ))

    def test_p0_contract_hash_tamper_invalidates_approval(self):
        contract = approve_contract(_contract(), "owner")
        self.assertTrue(contract_has_valid_approval(contract))
        contract.objective = "tampered objective"
        self.assertFalse(contract_has_valid_approval(contract))


class TestP0LifecycleRegression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(os.path.join(self.temp_dir.name, "runtime.sqlite3"), legacy_tasks_file=None)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_p0_cannot_close_without_acceptance(self):
        run_id = self.store.start_run("thread", "objective", "managed_task")
        self.assertFalse(self.store.close_run(run_id, "owner", "accepted"))
        self.assertEqual(self.store.get_run(run_id)["status"], "running")

    def test_p0_plan_and_acceptance_gates_are_one_time(self):
        run_id = self.store.start_run("thread", "objective", "managed_task")
        self.assertTrue(self.store.acknowledge_plan(run_id, "owner"))
        self.assertFalse(self.store.acknowledge_plan(run_id, "owner"))
        self.assertTrue(self.store.decide_acceptance(run_id, "accepted", "owner"))
        self.assertFalse(self.store.decide_acceptance(run_id, "rejected", "owner"))

    def test_p0_runtime_schema_migrates_old_runs_table(self):
        path = os.path.join(self.temp_dir.name, "old.sqlite3")
        with sqlite3.connect(path) as conn:
            conn.execute("""
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, objective TEXT NOT NULL,
                    mode TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
                    contract_id TEXT, contract_hash TEXT, started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, completed_at TEXT, error TEXT
                )
            """)
        store = RuntimeStore(path, legacy_tasks_file=None)
        columns = {row[1] for row in store._connect().execute("PRAGMA table_info(runs)")}
        self.assertIn("task_id", columns)
        self.assertIn("effective_policy_json", columns)
        self.assertIn("reconciliation_json", columns)


class TestP0PlannerAndEvidenceRegression(unittest.TestCase):
    def test_p0_planner_rejects_malformed_shape(self):
        with self.assertRaises(ValueError):
            validate_model_plan({"not_tasks": []}, "task", max_children=4)

    def test_p0_planner_rejects_duplicate_ids(self):
        with self.assertRaises(ValueError):
            validate_model_plan([
                {"id": "same", "objective": "first"},
                {"id": "same", "objective": "second"},
            ], "task", max_children=4)

    def test_p0_planner_rejects_forward_or_unknown_dependencies(self):
        with self.assertRaises(ValueError):
            validate_model_plan([
                {"id": "first", "objective": "first", "depends_on": ["later"]},
            ], "task", max_children=4)

    def test_p0_planner_rejects_excessive_task_count(self):
        with self.assertRaises(ValueError):
            validate_model_plan(
                [{"objective": f"step-{index}"} for index in range(5)],
                "task",
                max_children=4,
            )

    def test_p0_reconciliation_flags_scope_violation_and_unreported_change(self):
        result = reconcile_changes(
            {"added": ["reports/result.txt", "outside.txt"], "modified": [], "deleted": []},
            ["reports/**"],
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("scope_violation", result["categories"])
        self.assertIn("unreported_change", result["categories"])


if __name__ == "__main__":
    unittest.main()

