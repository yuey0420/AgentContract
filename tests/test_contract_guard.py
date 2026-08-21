import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pactflow.core.contracts.guard import guard_tool_call
from pactflow.core.contracts.models import TaskContract
from pactflow.core.contracts.store import approve_contract


def make_contract(
    status="approved",
    *,
    can_execute=None,
    high_risk_tools=None,
    require_confirmation_for=None,
):
    contract = TaskContract.model_validate({
        "contract_version": "0.1",
        "id": "contract-guard",
        "owner": "local_user",
        "objective": "测试守卫",
        "risk_level": "high",
        "status": status,
        "scope": {
            "can_write": ["reports/**"],
            "cannot_write": ["skills/**"],
            "can_execute": can_execute or ["python"],
            "cannot_execute": ["rm -rf"],
        },
        "tool_policy": {
            "allowed_tools": ["write_office_file", "execute_office_shell"],
            "blocked_tools": [],
            "high_risk_tools": high_risk_tools or [],
            "require_confirmation_for": require_confirmation_for or [],
        },
    })
    return approve_contract(contract, "test-owner") if status == "approved" else contract


class TestContractGuard(unittest.TestCase):
    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract", return_value=None)
    def test_no_active_contract_allows_compatibility(self, _mock_load, mock_log):
        decision = guard_tool_call("test-thread", "write_office_file", {"filepath": "anything.txt"})
        self.assertEqual(decision.decision, "allow")
        self.assertIsNone(decision.contract_id)
        mock_log.assert_called()

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract", return_value=None)
    def test_no_contract_execute_requires_confirmation(self, _mock_load, _mock_log):
        decision = guard_tool_call(
            "test-thread",
            "execute_office_shell",
            {"command": "python script.py"},
            capability="execute",
        )
        self.assertEqual(decision.decision, "require_confirmation")
        self.assertEqual(decision.clause, "runtime.baseline_confirmation")

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract", return_value=make_contract(status="draft"))
    def test_draft_contract_denies(self, _mock_load, mock_log):
        decision = guard_tool_call("test-thread", "write_office_file", {"filepath": "reports/a.md"})
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "contract.status")
        self.assertTrue(any(call.kwargs.get("event") == "contract_violation" for call in mock_log.call_args_list))

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract")
    def test_modified_approved_contract_denies(self, mock_load, _mock_log):
        contract = make_contract()
        contract.objective = "tampered"
        mock_load.return_value = contract
        decision = guard_tool_call("test-thread", "write_office_file", {"filepath": "reports/a.md"})
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "contract.approval.contract_hash")

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract", return_value=make_contract())
    def test_write_allowed_path(self, _mock_load, _mock_log):
        decision = guard_tool_call("test-thread", "write_office_file", {"filepath": "reports/a.md"})
        self.assertEqual(decision.decision, "allow")

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract", return_value=make_contract())
    def test_write_blocked_path(self, _mock_load, mock_log):
        decision = guard_tool_call("test-thread", "write_office_file", {"filepath": "skills/a.py"})
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "scope.cannot_write")
        self.assertTrue(any(call.kwargs.get("event") == "contract_violation" for call in mock_log.call_args_list))

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract", return_value=make_contract())
    def test_shell_allowed_command(self, _mock_load, _mock_log):
        decision = guard_tool_call("test-thread", "execute_office_shell", {"command": "python script.py"})
        self.assertEqual(decision.decision, "allow")

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    @patch("pactflow.core.contracts.guard.load_active_contract", return_value=make_contract())
    def test_shell_blocked_command(self, _mock_load, _mock_log):
        decision = guard_tool_call("test-thread", "execute_office_shell", {"command": "rm -rf tmp"})
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "scope.cannot_execute")

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    def test_high_risk_tools_require_confirmation(self, _mock_log):
        contract = make_contract(high_risk_tools=["write_office_file"])
        with patch("pactflow.core.contracts.guard.load_active_contract", return_value=contract):
            decision = guard_tool_call(
                "test-thread", "write_office_file", {"filepath": "reports/a.md"},
                capability="write", resource="reports/a.md",
            )
        self.assertEqual(decision.decision, "require_confirmation")
        self.assertEqual(decision.clause, "tool_policy.high_risk_tools")

    @patch("pactflow.core.contracts.guard.audit_logger.log_event")
    def test_read_only_shell_skips_coarse_tool_confirmation(self, _mock_log):
        contract = make_contract(
            can_execute=["git status"],
            require_confirmation_for=["execute_office_shell"],
        )
        with patch("pactflow.core.contracts.guard.load_active_contract", return_value=contract):
            decision = guard_tool_call(
                "test-thread", "execute_office_shell", {"command": "git status"},
                capability="execute", resource="git status", effect="read",
            )
        self.assertEqual(decision.decision, "allow")


if __name__ == "__main__":
    unittest.main()
