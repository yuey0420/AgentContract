import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cyberclaw.core.contracts.models import TaskContract
from cyberclaw.core.contracts.policy import (
    check_resource_boundary,
    check_shell_command,
    check_tool_allowed,
    check_write_path,
)


def make_contract():
    return TaskContract.model_validate({
        "contract_version": "0.1",
        "id": "contract-policy",
        "owner": "local_user",
        "objective": "测试策略",
        "risk_level": "medium",
        "status": "approved",
        "scope": {
            "can_write": ["reports/**", "summary.md"],
            "cannot_write": ["reports/private/**", "skills/**"],
            "can_execute": ["python", "pytest", "ls"],
            "cannot_execute": ["rm -rf", "git push", "curl"],
        },
        "tool_policy": {
            "allowed_tools": ["write_office_file", "execute_office_shell"],
            "blocked_tools": ["save_user_profile"],
        },
    })


class TestContractPolicy(unittest.TestCase):
    def test_tool_allowed(self):
        decision = check_tool_allowed(make_contract(), "write_office_file")
        self.assertEqual(decision.decision, "allow")

    def test_tool_blocked(self):
        decision = check_tool_allowed(make_contract(), "save_user_profile")
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "tool_policy.blocked_tools")

    def test_write_cannot_write_wins(self):
        decision = check_write_path(make_contract(), "reports/private/a.md")
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "scope.cannot_write")

    def test_write_allowed_path(self):
        decision = check_write_path(make_contract(), "reports/a.md")
        self.assertEqual(decision.decision, "allow")

    def test_write_unmatched_path_denied(self):
        decision = check_write_path(make_contract(), "other/a.md")
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "scope.can_write")

    def test_shell_allowed_command(self):
        decision = check_shell_command(make_contract(), "python script.py")
        self.assertEqual(decision.decision, "allow")

    def test_shell_blocked_command(self):
        decision = check_shell_command(make_contract(), "git push origin main")
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "scope.cannot_execute")

    def test_shell_unmatched_command_denied(self):
        decision = check_shell_command(make_contract(), "node build.js")
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "scope.can_execute")

    def test_forbidden_boundary_denies(self):
        contract = make_contract()
        contract.scope.forbidden = ["framework/**"]
        decision = check_resource_boundary(contract, "framework/core.py")
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.clause, "scope.forbidden")

    def test_human_boundary_requires_confirmation(self):
        contract = make_contract()
        contract.scope.human_only = ["models/**"]
        decision = check_resource_boundary(contract, "models/User.py")
        self.assertEqual(decision.decision, "require_confirmation")
        self.assertEqual(decision.clause, "scope.human_only")


if __name__ == "__main__":
    unittest.main()
