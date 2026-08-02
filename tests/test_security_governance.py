import os
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from cyberclaw.core.contracts.instructions import InstructionEnvelope
from cyberclaw.core.contracts.models import ContractDecision
from cyberclaw.core.contracts.security_policy import SecurityPolicyRuntime
from cyberclaw.core.contracts.tool_node import ContractToolNode
from cyberclaw.core.runtime_store import RuntimeStore


class TestSecurityGovernance(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(
            os.path.join(self.temp_dir.name, "runtime.sqlite3"),
            legacy_tasks_file=None,
        )
        self.run_id = self.store.start_run("security-thread", "data flow", "chat")
        self.config = {"configurable": {"thread_id": "security-thread"}}

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _instruction(**overrides) -> InstructionEnvelope:
        data = {
            "instruction_id": "instruction-1",
            "run_id": "run-1",
            "thread_id": "thread-1",
            "tool_call_id": "call-2",
            "tool_name": "side_effect",
            "args": {"value": "derived"},
            "capability": "write",
            "reference_tool_ids": ["call-1"],
            "trustworthiness": "untrusted",
            "confidentiality": "public",
            "risk_level": "medium",
        }
        data.update(overrides)
        return InstructionEnvelope.model_validate(data)

    def test_untrusted_side_effect_requires_confirmation(self):
        evaluation = SecurityPolicyRuntime(mode="enforce").check(self._instruction())

        self.assertEqual(evaluation.decision, "require_confirmation")
        self.assertEqual(evaluation.effective_decision, "require_confirmation")
        self.assertEqual(evaluation.primary_finding.policy, "security.untrusted_side_effect")

    def test_confidential_external_flow_is_denied(self):
        evaluation = SecurityPolicyRuntime(mode="enforce").check(self._instruction(
            capability="external",
            confidentiality="confidential",
            risk_level="high",
        ))

        self.assertEqual(evaluation.effective_decision, "deny")
        self.assertEqual(evaluation.primary_finding.policy, "security.confidential_external_flow")

    def test_observe_mode_reports_without_enforcing(self):
        evaluation = SecurityPolicyRuntime(mode="observe").check(self._instruction())

        self.assertEqual(evaluation.decision, "require_confirmation")
        self.assertEqual(evaluation.effective_decision, "allow")

    def test_instruction_store_keeps_lineage_and_redacts_content(self):
        instruction = self._instruction(
            instruction_id="persisted",
            run_id=self.run_id,
            args={"content": "sensitive payload", "token": "secret"},
        )
        self.store.record_instruction(instruction.model_dump())
        self.store.update_instruction(
            instruction.instruction_id,
            status="succeeded",
            result_trustworthiness="trusted",
            result_confidentiality="internal",
        )

        stored = self.store.get_run_instructions(self.run_id)[0]
        self.assertEqual(stored["reference_tool_ids"], ["call-1"])
        self.assertEqual(stored["args"]["token"], "[REDACTED]")
        self.assertEqual(stored["args"]["content"]["length"], len("sensitive payload"))
        self.assertEqual(stored["status"], "succeeded")

    def test_untrusted_tool_result_requires_approval_before_write(self):
        executions: list[str] = []

        @tool
        def source_tool() -> str:
            """Return data from a low-trust source."""
            return "remote-value"

        source_tool.metadata = {
            "capability": "read",
            "result_trustworthiness": "untrusted",
            "result_confidentiality": "public",
        }

        @tool
        def sink_tool(value: str) -> str:
            """Persist a derived value."""
            executions.append(value)
            return f"stored:{value}"

        sink_tool.metadata = {"capability": "write"}
        node = ContractToolNode(
            [source_tool, sink_tool],
            security_runtime=SecurityPolicyRuntime(mode="enforce"),
        )
        source_call = AIMessage(content="", tool_calls=[{
            "name": "source_tool",
            "args": {},
            "id": "source-call",
            "type": "tool_call",
        }])

        with patch("cyberclaw.core.contracts.tool_node.runtime_store", self.store), \
             patch("cyberclaw.core.contracts.tool_node.load_active_contract", return_value=None), \
             patch(
                 "cyberclaw.core.contracts.tool_node.guard_tool_call",
                 return_value=ContractDecision(decision="allow", reason="ok"),
             ):
            source_result = node(
                {"run_id": self.run_id, "messages": [source_call]},
                self.config,
            )["messages"][0]

            sink_call = AIMessage(content="", tool_calls=[{
                "name": "sink_tool",
                "args": {"value": "remote-value"},
                "id": "sink-call",
                "type": "tool_call",
            }])

            def approve_interrupted_call(_payload):
                action = self.store.list_pending_actions("security-thread")[0]
                self.assertTrue(self.store.approve_pending_action(action["action_id"], "reviewer"))
                return {"action_id": action["action_id"]}

            with patch(
                "cyberclaw.core.contracts.tool_node.interrupt",
                side_effect=approve_interrupted_call,
            ):
                node(
                    {
                        "run_id": self.run_id,
                        "messages": [source_call, source_result, sink_call],
                    },
                    self.config,
                )

        self.assertEqual(executions, ["remote-value"])
        instructions = self.store.get_run_instructions(self.run_id)
        sink_instruction = next(
            item for item in instructions if item["tool_call_id"] == "sink-call"
        )
        self.assertEqual(sink_instruction["reference_tool_ids"], ["source-call"])
        self.assertIsNotNone(sink_instruction["parent_instruction_id"])
        self.assertEqual(sink_instruction["status"], "succeeded")
        events = [event["event"] for event in self.store.get_run_events(self.run_id)]
        self.assertIn("security_confirmation_required", events)
        self.assertIn("approval_consumed", events)


if __name__ == "__main__":
    unittest.main()
