import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from pactflow.core.contracts.models import ContractDecision, TaskContract
from pactflow.core.contracts.store import approve_contract
from pactflow.core.contracts.tool_node import ContractToolNode
from pactflow.core.runtime_store import RuntimeStore
from pactflow.core.approval import ApprovalService


class TestContractToolNode(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(os.path.join(self.temp_dir.name, "runtime.sqlite3"), legacy_tasks_file=None)
        self.run_id = self.store.start_run("thread-1", "test", "chat")

        @tool
        def sample_tool(value: str) -> str:
            """Return a value."""
            return f"ok:{value}"

        self.node = ContractToolNode([sample_tool])
        self.state = {
            "run_id": self.run_id,
            "messages": [AIMessage(content="", tool_calls=[{
                "name": "sample_tool",
                "args": {"value": "x"},
                "id": "call-1",
                "type": "tool_call",
            }])],
        }
        self.config = {"configurable": {"thread_id": "thread-1"}}

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None)
    @patch("pactflow.core.contracts.tool_node.guard_tool_call")
    def test_allowed_tool_runs_and_records_evidence(self, mock_guard, _mock_load):
        mock_guard.return_value = ContractDecision(decision="allow", reason="ok")
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store):
            result = self.node(self.state, self.config)

        self.assertEqual(result["messages"][0].content, "ok:x")
        events = self.store.get_run_events(self.run_id)
        self.assertEqual([event["event"] for event in events], ["tool_requested", "tool_succeeded"])

    @patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None)
    @patch("pactflow.core.contracts.tool_node.guard_tool_call")
    def test_denied_tool_never_executes(self, mock_guard, _mock_load):
        mock_guard.return_value = ContractDecision(decision="deny", clause="test", reason="blocked")
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store):
            result = self.node(self.state, self.config)

        self.assertIn("契约拒绝", result["messages"][0].content)
        events = self.store.get_run_events(self.run_id)
        self.assertEqual([event["event"] for event in events], ["tool_requested", "tool_denied"])

    @patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None)
    @patch("pactflow.core.contracts.tool_node.guard_tool_call")
    def test_confirmation_consumes_exact_call_once(self, mock_guard, _mock_load):
        mock_guard.return_value = ContractDecision(
            decision="require_confirmation",
            contract_id="contract-1",
            contract_hash=None,
            reason="confirm",
        )
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store):
            def approve_interrupted_call(_payload):
                action = self.store.list_pending_actions("thread-1")[0]
                self.assertTrue(self.store.approve_pending_action(action["action_id"], "tester"))
                return {"action_id": action["action_id"]}

            with patch("pactflow.core.contracts.tool_node.interrupt", side_effect=approve_interrupted_call):
                result = self.node(self.state, self.config)

        self.assertEqual(result["messages"][0].content, "ok:x")
        action = self.store.list_run_actions(self.run_id)[0]
        self.assertEqual(action["status"], "consumed")
        self.assertFalse(self.store.approve_pending_action(action["action_id"], "tester"))

    @patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None)
    @patch("pactflow.core.contracts.tool_node.guard_tool_call")
    def test_multiple_calls_are_not_executed_before_approval_batch_completes(
        self, mock_guard, _mock_load
    ):
        executions: list[str] = []

        @tool
        def batch_tool(value: str) -> str:
            """Record a batch value."""
            executions.append(value)
            return f"ok:{value}"

        state = {
            "run_id": self.run_id,
            "messages": [AIMessage(content="", tool_calls=[
                {"name": "batch_tool", "args": {"value": "first"}, "id": "batch-1", "type": "tool_call"},
                {"name": "batch_tool", "args": {"value": "second"}, "id": "batch-2", "type": "tool_call"},
            ])],
        }
        mock_guard.side_effect = [
            ContractDecision(decision="allow", reason="ok"),
            ContractDecision(decision="require_confirmation", clause="test", reason="confirm"),
        ]
        node = ContractToolNode([batch_tool])
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store), \
             patch("pactflow.core.contracts.tool_node.interrupt", side_effect=RuntimeError("paused")):
            with self.assertRaisesRegex(RuntimeError, "paused"):
                node(state, self.config)

        self.assertEqual(executions, [])

    @patch("pactflow.core.contracts.tool_node.guard_tool_call")
    def test_tool_call_limit_is_enforced(self, mock_guard):
        contract = TaskContract.model_validate({
            "contract_version": "0.2",
            "id": "limited",
            "owner": "tester",
            "objective": "limit",
            "status": "draft",
            "scope": {},
            "tool_policy": {"allowed_tools": ["sample_tool"]},
            "limits": {"max_tool_calls": 0},
        })
        contract = approve_contract(contract, "tester")
        mock_guard.return_value = ContractDecision(
            decision="allow",
            contract_id=contract.id,
            contract_hash=contract.approval.contract_hash,
            reason="ok",
        )
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store), \
             patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=contract):
            result = self.node(self.state, self.config)

        self.assertIn("契约拒绝", result["messages"][0].content)
        self.assertIn("max_tool_calls", result["messages"][0].content)

    @patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None)
    @patch("pactflow.core.contracts.tool_node.guard_tool_call")
    def test_error_string_is_recorded_as_failure(self, mock_guard, _mock_load):
        from langchain_core.tools import tool

        @tool
        def failing_tool() -> str:
            """Return a domain error."""
            return "失败：无法完成"

        state = {
            "run_id": self.run_id,
            "messages": [AIMessage(content="", tool_calls=[{
                "name": "failing_tool", "args": {}, "id": "call-2", "type": "tool_call",
            }])],
        }
        mock_guard.return_value = ContractDecision(decision="allow", reason="ok")
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store):
            ContractToolNode([failing_tool])(state, self.config)

        self.assertEqual(self.store.get_run_events(self.run_id)[-1]["event"], "tool_failed")

    @patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None)
    @patch("pactflow.core.contracts.tool_node.guard_tool_call")
    def test_matching_scoped_grant_skips_interrupt(self, mock_guard, _mock_load):
        decision = ContractDecision(
            decision="require_confirmation",
            contract_id="contract-1",
            contract_hash="sha256:test",
            clause="tool_policy.high_risk_tools",
            reason="confirm",
            risk_level="high",
        )
        mock_guard.return_value = decision
        self.state["process_profile"] = "development"
        self.store.create_approval_grant(
            run_id=self.run_id,
            thread_id="thread-1",
            contract_hash="sha256:test",
            tool_name="sample_tool",
            capability="external",
            resource_pattern="",
            approved_by="tester",
        )
        # External capabilities are never scope eligible, even with a forged grant.
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store), \
             patch("pactflow.core.contracts.tool_node.interrupt", side_effect=RuntimeError("paused")):
            with self.assertRaisesRegex(RuntimeError, "paused"):
                self.node(self.state, self.config)

        self.node.tools["sample_tool"].metadata = {
            "capability": "write",
            "resource": "reports/a.md",
        }
        self.store.create_approval_grant(
            run_id=self.run_id,
            thread_id="thread-1",
            contract_hash="sha256:test",
            tool_name="sample_tool",
            capability="write",
            resource_pattern="reports/**",
            approved_by="tester",
        )
        self.state["messages"] = [AIMessage(content="", tool_calls=[{
            "name": "sample_tool", "args": {"value": "y"},
            "id": "call-scope", "type": "tool_call",
        }])]
        with patch("pactflow.core.contracts.tool_node.runtime_store", self.store), \
             patch("pactflow.core.contracts.tool_node.interrupt", side_effect=RuntimeError("paused")):
            with self.assertRaisesRegex(RuntimeError, "paused"):
                self.node(self.state, self.config)
        self.assertEqual(self.store.list_approval_grants(self.run_id)[-1]["uses"], 0)

    def test_critical_action_cannot_create_scope(self):
        action_id = self.store.create_pending_action(
            self.run_id,
            "thread-1",
            "dangerous",
            {},
            "sha256:test",
            tool_call_id="critical-call",
            risk_level="critical",
            clause="security.critical_action",
            capability="execute",
            resource="rm data.txt",
            process_profile="development",
            effect="destructive",
        )
        service = ApprovalService(self.store)
        result = service.approve_scope(action_id, "tester")
        self.assertEqual(result.status, "approved")
        self.assertEqual(self.store.list_approval_grants(self.run_id), [])


if __name__ == "__main__":
    unittest.main()
