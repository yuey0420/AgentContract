import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cyberclaw.core.contracts.models import ContractDecision
from cyberclaw.core.skill_loader import LazySkillLoader, _viewed_skill_help


class TestContractSkillLoader(unittest.TestCase):
    def setUp(self):
        _viewed_skill_help.clear()

    def _make_skill_dir(self, root):
        skill_dir = os.path.join(root, "demo")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("name: demo_skill\ndescription: 演示技能\n\n运行 demo。")
        return skill_dir

    @patch("cyberclaw.core.skill_loader.audit_logger.log_event")
    def test_run_without_help_denied(self, _mock_log):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_skill_dir(tmp)
            with patch("cyberclaw.core.skill_loader.SKILLS_DIR", tmp):
                tools = LazySkillLoader().get_all_tools(force_rescan=True)
                result = tools[0].func(mode="run", command="python script.py")

        self.assertIn("契约拒绝：动态技能必须先 help 再 run", result)

    @patch("cyberclaw.core.skill_loader.guard_tool_call")
    @patch("cyberclaw.core.skill_loader.audit_logger.log_event")
    def test_help_then_run_enters_guard(self, _mock_log, mock_guard):
        mock_guard.return_value = ContractDecision(decision="allow", reason="ok")
        fake_shell = SimpleNamespace(invoke=lambda _args: "ok")

        with tempfile.TemporaryDirectory() as tmp:
            self._make_skill_dir(tmp)
            with patch("cyberclaw.core.skill_loader.SKILLS_DIR", tmp), \
                 patch("cyberclaw.core.skill_loader.execute_office_shell", fake_shell):
                tools = LazySkillLoader().get_all_tools(force_rescan=True)
                help_result = tools[0].func(mode="help")
                run_result = tools[0].func(mode="run", command="python {baseDir}/script.py")

        self.assertIn("完整说明书", help_result)
        self.assertEqual(run_result, "ok")
        mock_guard.assert_called()

    @patch("cyberclaw.core.skill_loader.guard_tool_call")
    @patch("cyberclaw.core.skill_loader.audit_logger.log_event")
    def test_help_then_run_blocked_by_contract(self, _mock_log, mock_guard):
        mock_guard.return_value = ContractDecision(
            decision="deny",
            contract_id="contract-skill",
            clause="scope.cannot_execute",
            reason="blocked",
        )

        with tempfile.TemporaryDirectory() as tmp:
            self._make_skill_dir(tmp)
            with patch("cyberclaw.core.skill_loader.SKILLS_DIR", tmp):
                tools = LazySkillLoader().get_all_tools(force_rescan=True)
                tools[0].func(mode="help")
                result = tools[0].func(mode="run", command="curl http://example.com")

        self.assertIn("契约拒绝", result)
        self.assertIn("blocked", result)


if __name__ == "__main__":
    unittest.main()
