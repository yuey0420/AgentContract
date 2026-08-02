import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pactflow.core.skill_loader import LazySkillLoader, _viewed_skill_help


class TestContractSkillLoader(unittest.TestCase):
    def setUp(self):
        _viewed_skill_help.clear()

    def _make_skill_dir(self, root):
        skill_dir = os.path.join(root, "demo")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("name: demo_skill\ndescription: 演示技能\n\n运行 demo。")
        return skill_dir

    @patch("pactflow.core.skill_loader.audit_logger.log_event")
    def test_run_without_help_denied(self, _mock_log):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_skill_dir(tmp)
            with patch("pactflow.core.skill_loader.SKILLS_DIR", tmp):
                tools = LazySkillLoader().get_all_tools(force_rescan=True)
                result = tools[0].func(mode="run", command="python script.py")

        self.assertIn("契约拒绝：动态技能必须先 help 再 run", result)

    @patch("pactflow.core.skill_loader.audit_logger.log_event")
    def test_help_then_run_executes_restricted_runner(self, _mock_log):
        fake_shell = SimpleNamespace(invoke=lambda _args: "ok")

        with tempfile.TemporaryDirectory() as tmp:
            self._make_skill_dir(tmp)
            with patch("pactflow.core.skill_loader.SKILLS_DIR", tmp), \
                 patch("pactflow.core.skill_loader.execute_office_shell", fake_shell):
                tools = LazySkillLoader().get_all_tools(force_rescan=True)
                help_result = tools[0].func(mode="help")
                run_result = tools[0].func(mode="run", command="python {baseDir}/script.py")

        self.assertIn("完整说明书", help_result)
        self.assertEqual(run_result, "ok")


if __name__ == "__main__":
    unittest.main()
