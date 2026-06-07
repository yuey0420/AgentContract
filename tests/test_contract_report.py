import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cyberclaw.core.contracts.models import TaskContract
from cyberclaw.core.contracts.report import generate_contract_report


def make_contract():
    return TaskContract.model_validate({
        "contract_version": "0.1",
        "id": "contract-report",
        "owner": "local_user",
        "objective": "测试报告",
        "risk_level": "medium",
        "status": "approved",
        "scope": {
            "can_write": ["reports/**"],
            "can_execute": ["python"],
        },
        "tool_policy": {
            "allowed_tools": ["write_office_file", "execute_office_shell"],
        },
        "acceptance": [
            {"type": "no_contract_violation"},
            {"type": "tool_called", "tool": "write_office_file"},
            {"type": "file_exists", "path": "reports/summary.md"},
        ],
    })


class TestContractReport(unittest.TestCase):
    @patch("cyberclaw.core.contracts.report.write_report", return_value="report.json")
    @patch("cyberclaw.core.contracts.report._read_log_events")
    def test_report_passes_without_violation(self, mock_events, _mock_write):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "reports"))
            with open(os.path.join(tmp, "reports", "summary.md"), "w", encoding="utf-8") as f:
                f.write("summary")

            mock_events.return_value = [
                {
                    "event": "contract_check",
                    "contract_id": "contract-report",
                    "decision": "allow",
                    "tool": "write_office_file",
                }
            ]

            with patch("cyberclaw.core.contracts.report.OFFICE_DIR", tmp):
                report = generate_contract_report(make_contract(), thread_id="test-thread")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["violations"], 0)

    @patch("cyberclaw.core.contracts.report.write_report", return_value="report.json")
    @patch("cyberclaw.core.contracts.report._read_log_events")
    def test_report_fails_with_violation(self, mock_events, _mock_write):
        mock_events.return_value = [
            {
                "event": "contract_violation",
                "contract_id": "contract-report",
                "decision": "deny",
                "tool": "write_office_file",
            }
        ]

        report = generate_contract_report(make_contract(), thread_id="test-thread")

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["violations"], 1)


if __name__ == "__main__":
    unittest.main()
