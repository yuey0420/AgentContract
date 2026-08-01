import os
import tempfile
import unittest
from unittest.mock import patch

from cyberclaw.core.contracts.models import TaskContract
from cyberclaw.core.contracts.store import approve_contract
from cyberclaw.core.process.manager import ProcessManager
from cyberclaw.core.runtime_store import RuntimeStore


class TestProcessManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(os.path.join(self.temp_dir.name, "runtime.sqlite3"), legacy_tasks_file=None)
        self.manager = ProcessManager(self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("cyberclaw.core.process.manager.write_report")
    @patch("cyberclaw.core.process.manager.load_active_contract", return_value=None)
    def test_chat_run_is_started_and_finalized(self, _mock_load, _mock_write):
        state = self.manager.start("thread-1", "hello")
        report = self.manager.finalize(state["run_id"], "thread-1")

        run = self.store.get_run(state["run_id"])
        self.assertEqual(state["execution_mode"], "chat")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(run["status"], "completed")

    @patch("cyberclaw.core.contracts.report.write_report")
    def test_managed_run_uses_run_scoped_acceptance(self, _mock_write):
        contract = TaskContract.model_validate({
            "contract_version": "0.2",
            "id": "managed",
            "owner": "tester",
            "objective": "write report",
            "status": "draft",
            "scope": {"can_write": ["reports/**"]},
            "tool_policy": {"allowed_tools": ["write_office_file"]},
            "acceptance": [{"type": "tool_called", "tool": "write_office_file"}],
        })
        contract = approve_contract(contract, "tester")

        with patch("cyberclaw.core.process.manager.load_active_contract", return_value=contract):
            state = self.manager.start("thread-1", "write")
            self.store.append_run_event(state["run_id"], "tool_succeeded", {
                "tool": "write_office_file",
                "capability": "write",
                "resource": "reports/a.md",
            })
            report = self.manager.finalize(state["run_id"], "thread-1")

        self.assertEqual(state["execution_mode"], "managed_task")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["run_id"], state["run_id"])

    @patch("cyberclaw.core.process.manager.write_report")
    def test_contract_change_makes_run_inconclusive(self, _mock_write):
        contract = TaskContract.model_validate({
            "contract_version": "0.2",
            "id": "changed",
            "owner": "tester",
            "objective": "before",
            "status": "draft",
            "scope": {},
            "tool_policy": {},
        })
        contract = approve_contract(contract, "tester")
        changed = contract.model_copy(deep=True)
        changed.objective = "after"

        with patch("cyberclaw.core.process.manager.load_active_contract", side_effect=[contract, changed]):
            state = self.manager.start("thread-1", "change")
            report = self.manager.finalize(state["run_id"], "thread-1")

        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(self.store.get_run(state["run_id"])["status"], "needs_review")

    @patch("cyberclaw.core.process.manager.write_report")
    @patch("cyberclaw.core.process.manager.load_active_contract", return_value=None)
    def test_pending_approval_never_passes(self, _mock_load, _mock_write):
        state = self.manager.start("thread-1", "dangerous action")
        self.store.append_run_event(state["run_id"], "approval_required", {"action_id": "a1"})
        report = self.manager.finalize(state["run_id"], "thread-1")
        self.assertEqual(report["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
