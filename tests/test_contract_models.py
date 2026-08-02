import os
import sys
import unittest

from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pactflow.core.contracts.models import TaskContract


def sample_contract_data():
    return {
        "contract_version": "0.1",
        "id": "contract-test",
        "owner": "local_user",
        "objective": "测试契约",
        "risk_level": "medium",
        "status": "approved",
        "scope": {
            "can_write": ["reports/**"],
            "cannot_write": ["skills/**"],
            "can_execute": ["python", "pytest"],
            "cannot_execute": ["rm -rf"],
        },
        "tool_policy": {
            "allowed_tools": ["write_office_file", "execute_office_shell"],
            "blocked_tools": ["save_user_profile"],
            "high_risk_tools": ["write_office_file"],
            "require_confirmation_for": ["execute_office_shell"],
        },
    }


class TestContractModels(unittest.TestCase):
    def test_valid_contract_parses(self):
        contract = TaskContract.model_validate(sample_contract_data())
        self.assertEqual(contract.id, "contract-test")
        self.assertEqual(contract.status, "approved")
        self.assertEqual(contract.limits.max_shell_seconds, 60)

    def test_missing_required_field_fails(self):
        data = sample_contract_data()
        del data["objective"]
        with self.assertRaises(ValidationError):
            TaskContract.model_validate(data)

    def test_invalid_risk_level_fails(self):
        data = sample_contract_data()
        data["risk_level"] = "extreme"
        with self.assertRaises(ValidationError):
            TaskContract.model_validate(data)

    def test_unknown_contract_field_fails(self):
        data = sample_contract_data()
        data["unknown_policy"] = True
        with self.assertRaises(ValidationError):
            TaskContract.model_validate(data)

    def test_invalid_limit_fails(self):
        data = sample_contract_data()
        data["limits"] = {"max_shell_seconds": 0}
        with self.assertRaises(ValidationError):
            TaskContract.model_validate(data)


if __name__ == "__main__":
    unittest.main()
