import unittest

from langchain_core.tools import tool

from pactflow.core.contracts.action_risk import classify_shell_effect, shell_risk_level
from pactflow.core.contracts.instructions import classify_tool_result


class TestActionRisk(unittest.TestCase):
    def test_read_only_commands_are_low_risk(self):
        for command in ("dir", "ls reports", "git status", "git diff", "pytest -q"):
            with self.subTest(command=command):
                self.assertEqual(classify_shell_effect(command), "read")
                self.assertEqual(shell_risk_level(command), "low")

    def test_destructive_and_external_commands_are_critical(self):
        for command, effect in (("rm report.txt", "destructive"), ("curl example.com", "external")):
            with self.subTest(command=command):
                self.assertEqual(classify_shell_effect(command), effect)
                self.assertEqual(shell_risk_level(command), "critical")

    def test_known_local_reads_are_trusted_but_external_results_are_not(self):
        @tool("read_office_file")
        def local_read(filepath: str) -> str:
            """Read a sandbox file."""
            return filepath

        @tool("external_source")
        def external_source(query: str) -> str:
            """Read an external source."""
            return query

        self.assertEqual(classify_tool_result(local_read, "read")[0], "trusted")
        self.assertEqual(classify_tool_result(external_source, "external")[0], "untrusted")


if __name__ == "__main__":
    unittest.main()
