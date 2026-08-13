import os
import sys
import tempfile
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pactflow.core.skill_loader import LazySkillLoader, _viewed_skill_help, _viewed_skill_manifest
from pactflow.core.contracts.tool_node import resolve_tool_intent


class TestContractSkillLoader(unittest.TestCase):
    def setUp(self):
        _viewed_skill_help.clear()
        _viewed_skill_manifest.clear()

    def _make_skill_dir(self, root, *, risk_level=None, trust_level=None, tags=None):
        skill_dir = os.path.join(root, "demo")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            lines = ["name: demo_skill", "description: 演示技能"]
            if risk_level:
                lines.append(f"risk_level: {risk_level}")
            if trust_level:
                lines.append(f"trust_level: {trust_level}")
            if tags:
                lines.append(f"tags: {json.dumps(tags, ensure_ascii=False)}")
            f.write("\n".join(lines) + "\n\n运行 demo。")
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

    @patch("pactflow.core.skill_loader.audit_logger.log_event")
    def test_manifest_allows_medium_skill_without_full_help(self, _mock_log):
        fake_shell = SimpleNamespace(invoke=lambda _args: "manifest-run-ok")

        with tempfile.TemporaryDirectory() as tmp:
            self._make_skill_dir(tmp, risk_level="medium", trust_level="external")
            with patch("pactflow.core.skill_loader.SKILLS_DIR", tmp), \
                 patch("pactflow.core.skill_loader.execute_office_shell", fake_shell):
                tools = LazySkillLoader().get_all_tools(force_rescan=True)
                manifest = tools[0].func(mode="manifest")
                run_result = tools[0].func(mode="run", command="python {baseDir}/script.py")
                intent = resolve_tool_intent(tools[0], {"mode": "manifest"})

        self.assertEqual(json.loads(manifest)["stage"], "manifest")
        self.assertIn("requires_full_help", manifest)
        self.assertEqual(run_result, "manifest-run-ok")
        self.assertEqual(intent.capability, "read")
        self.assertIn("skills/demo/SKILL.md", intent.resource)

    @patch("pactflow.core.skill_loader.audit_logger.log_event")
    def test_high_risk_skill_requires_full_help(self, _mock_log):
        fake_shell = SimpleNamespace(invoke=lambda _args: "high-risk-ok")

        with tempfile.TemporaryDirectory() as tmp:
            self._make_skill_dir(tmp, risk_level="high", trust_level="verified")
            with patch("pactflow.core.skill_loader.SKILLS_DIR", tmp), \
                 patch("pactflow.core.skill_loader.execute_office_shell", fake_shell):
                tools = LazySkillLoader().get_all_tools(force_rescan=True)
                tools[0].func(mode="manifest")
                denied = tools[0].func(mode="run", command="python {baseDir}/script.py")
                tools[0].func(mode="help")
                allowed = tools[0].func(mode="run", command="python {baseDir}/script.py")

        self.assertIn("必须完整 help", denied)
        self.assertEqual(allowed, "high-risk-ok")

    def test_retrieve_skill_manifests_ranks_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            weather_dir = os.path.join(tmp, "weather")
            os.makedirs(weather_dir)
            with open(os.path.join(weather_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(
                    "name: weather\n"
                    "description: 查询天气和天气预报\n"
                    "risk_level: low\n"
                    "trust_level: trusted\n"
                    "tags: [天气, forecast]\n"
                )
            other_dir = os.path.join(tmp, "other")
            os.makedirs(other_dir)
            with open(os.path.join(other_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("name: calculator\ndescription: 数学计算\n")

            with patch("pactflow.core.skill_loader.SKILLS_DIR", tmp):
                loader = LazySkillLoader()
                results = loader.retrieve_skill_manifests("查询天气", top_k=1, force_rescan=True)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "weather")
        self.assertEqual(results[0]["risk_level"], "low")
        self.assertIn("天气", results[0]["tags"])


if __name__ == "__main__":
    unittest.main()
