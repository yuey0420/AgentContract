import os
import tempfile
import unittest
from types import SimpleNamespace

from pactflow.core.contracts.models import TaskContract
from pactflow.core.process.capabilities import assess_contract_capabilities
from pactflow.core.process.evidence import (
    capture_filesystem_snapshot,
    diff_filesystem_snapshots,
    reconcile_changes,
)
from pactflow.core.process.manager import ProcessManager
from pactflow.core.process.task_planner import decompose_objective, model_decompose_objective
from pactflow.core.runtime_store import RuntimeStore


class TestProcessGovernance(unittest.TestCase):
    def test_process_profile_selects_planner_mode(self):
        base = {
            "id": "mode-test",
            "owner": "tester",
            "objective": "test",
            "scope": {},
            "tool_policy": {},
        }
        current = TaskContract.model_validate({**base, "contract_version": "2.0"})
        legacy = TaskContract.model_validate({**base, "contract_version": "0.2"})
        chat = TaskContract.model_validate({**base, "contract_version": "2.0", "process_profile": "chat"})
        self.assertEqual(ProcessManager.resolve_planner_mode(current), "model")
        self.assertEqual(ProcessManager.resolve_planner_mode(legacy), "deterministic")
        self.assertEqual(ProcessManager.resolve_planner_mode(chat), "none")
        self.assertEqual(ProcessManager.normalize_process_profile("managed"), "development")
        self.assertEqual(ProcessManager.normalize_process_profile("federated"), "audited")

    def test_model_planner_validates_and_normalizes_dag(self):
        contract = TaskContract.model_validate({
            "contract_version": "2.0",
            "id": "model-plan",
            "owner": "tester",
            "objective": "prepare and verify",
            "scope": {},
            "tool_policy": {},
            "planner_mode": "model",
        })

        class FakeModel:
            def invoke(self, _messages):
                return SimpleNamespace(content='{"tasks": [{"id": "extract", "objective": "Extract inputs"}, {"id": "verify", "objective": "Verify output", "depends_on": ["extract"]}]}')

        nodes, metadata = model_decompose_objective(FakeModel(), "prepare and verify", "task", contract)
        self.assertEqual([node["node_id"] for node in nodes], ["task", "extract", "verify"])
        self.assertEqual(nodes[2]["depends_on"], ["extract"])
        self.assertTrue(metadata["plan_hash"].startswith("sha256:"))

    def test_task_planner_creates_bounded_tree(self):
        nodes = decompose_objective("collect; write; verify", "task-1")
        self.assertEqual(
            [node["node_id"] for node in nodes],
            ["task-1", "task-1.1", "task-1.2", "task-1.3"],
        )
        self.assertEqual(nodes[1]["parent_node_id"], "task-1")

    def test_capability_assessment_separates_effective_policy(self):
        contract = TaskContract.model_validate({
            "contract_version": "2.0",
            "id": "capability-test",
            "owner": "tester",
            "objective": "test",
            "risk_level": "high",
            "scope": {
                "can_read": ["reports/**"],
                "can_write": ["reports/**", "private/**"],
                "cannot_write": ["private/**"],
                "forbidden": ["reports/secret/**"],
            },
            "tool_policy": {
                "allowed_tools": ["write_office_file", "shell"],
                "blocked_tools": ["shell"],
            },
        })
        assessment = assess_contract_capabilities(contract)
        self.assertIn("private/**", assessment["requested"]["write"])
        self.assertNotIn("private/**", assessment["effective"]["write"])
        self.assertNotIn("shell", assessment["effective"]["tools"])
        self.assertTrue(assessment["enforcement"]["approval_gate"])

    def test_filesystem_reconciliation_classifies_unreported_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            before = capture_filesystem_snapshot(directory)
            os.makedirs(os.path.join(directory, "reports"))
            with open(os.path.join(directory, "reports", "result.txt"), "w", encoding="utf-8") as file:
                file.write("ok")
            after = capture_filesystem_snapshot(directory)

        diff = diff_filesystem_snapshots(before, after)
        reconciliation = reconcile_changes(diff, ["reports/**"])
        self.assertEqual(reconciliation["status"], "needs_review")
        self.assertIn("unreported_change", reconciliation["categories"])

    def test_runtime_store_persists_governance_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(os.path.join(directory, "runtime.sqlite3"), legacy_tasks_file=None)
            run_id = store.start_run("thread", "objective", "managed_task")
            store.create_task_nodes(run_id, decompose_objective("one; two", "task"))
            store.update_task_node("task.1", status="running")
            store.create_checkpoint(run_id, "plan", "passed", "tool_generated", {"count": 2})
            store.register_repository(run_id, "main", directory, revision="abc")
            self.assertEqual(len(store.list_task_nodes(run_id)), 3)
            self.assertEqual(store.list_checkpoints(run_id)[0]["evidence"]["count"], 2)
            self.assertEqual(store.list_repositories(run_id)[0]["revision"], "abc")


if __name__ == "__main__":
    unittest.main()
