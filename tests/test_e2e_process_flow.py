import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from pactflow.core.agent import create_agent_app
from pactflow.core.contracts.models import TaskContract
from pactflow.core.contracts.store import approve_contract
from pactflow.core.process.manager import ProcessManager
from pactflow.core.runtime_store import RuntimeStore
from pactflow.core.tools.sandbox_tools import write_office_file


def _provider_with_responses(*responses: AIMessage) -> Mock:
    bound_model = Mock()
    bound_model.invoke.side_effect = list(responses)
    provider = Mock()
    provider.bind_tools.return_value = bound_model
    return provider


def _tool_call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": name,
        "args": args,
        "id": call_id,
        "type": "tool_call",
    }])


class ProcessE2ETestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.office_dir = os.path.join(self.temp_dir.name, "office")
        os.makedirs(self.office_dir)
        self.store = RuntimeStore(
            os.path.join(self.temp_dir.name, "runtime.sqlite3"),
            legacy_tasks_file=None,
        )
        self.manager = ProcessManager(self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_graph(self, provider: Mock, tools: list, contract: TaskContract | None):
        with ExitStack() as stack:
            stack.enter_context(patch("pactflow.core.agent.get_provider", return_value=provider))
            stack.enter_context(patch("pactflow.core.agent.process_manager", self.manager))
            stack.enter_context(patch("pactflow.core.process.manager.load_active_contract", return_value=contract))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=contract))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.runtime_store", self.store))
            stack.enter_context(patch("pactflow.core.contracts.guard.load_active_contract", return_value=contract))
            stack.enter_context(patch("pactflow.core.contracts.report.write_report", return_value="report.json"))
            stack.enter_context(patch("pactflow.core.process.manager.write_report", return_value="report.json"))
            stack.enter_context(patch("pactflow.core.contracts.report.OFFICE_DIR", self.office_dir))
            stack.enter_context(patch("pactflow.core.tools.sandbox_tools.OFFICE_DIR", self.office_dir))

            app = create_agent_app(tools=tools)
            return app.invoke(
                {"messages": [HumanMessage(content="execute e2e task")], "summary": ""},
                config={"configurable": {"thread_id": "e2e-thread"}},
            )

    @staticmethod
    def _write_contract(forbidden: list[str] | None = None) -> TaskContract:
        contract = TaskContract.model_validate({
            "contract_version": "0.2",
            "id": "e2e-write",
            "owner": "e2e-owner",
            "objective": "write a report",
            "status": "draft",
            "scope": {
                "can_write": ["reports/**"],
                "autonomous": ["reports/**"],
                "forbidden": forbidden or [],
            },
            "tool_policy": {"allowed_tools": ["write_office_file"]},
            "acceptance": [
                {"type": "no_contract_violation"},
                {"type": "tool_called", "tool": "write_office_file"},
                {"type": "file_exists", "path": "reports/result.txt"},
            ],
        })
        return approve_contract(contract, "e2e-owner")

    def test_managed_task_writes_and_auto_accepts(self):
        provider = _provider_with_responses(
            _tool_call("write_office_file", {
                "filepath": "reports/result.txt",
                "content": "verified",
                "mode": "w",
            }),
            AIMessage(content="done"),
        )

        state = self._run_graph(provider, [write_office_file], self._write_contract())

        output_path = os.path.join(self.office_dir, "reports", "result.txt")
        self.assertTrue(os.path.exists(output_path))
        with open(output_path, encoding="utf-8") as file:
            self.assertEqual(file.read(), "verified")
        self.assertEqual(state["execution_mode"], "managed_task")
        self.assertEqual(state["process_report"]["status"], "passed")
        events = self.store.get_run_events(state["run_id"])
        self.assertIn("tool_succeeded", [event["event"] for event in events])
        self.assertEqual(events[-1]["event"], "delivery.ready")
        self.assertEqual(state["process_report"]["delivery"]["human_acceptance"], "pending")

    def test_model_text_without_node_ac_does_not_complete_subtasks(self):
        provider = _provider_with_responses(
            AIMessage(content="first step complete"),
            AIMessage(content="second step complete"),
        )
        contract = TaskContract.model_validate({
            "contract_version": "2.0",
            "id": "subtask-flow",
            "owner": "e2e-owner",
            "objective": "first; second",
            "status": "approved",
            "planner_mode": "deterministic",
            "scope": {},
            "tool_policy": {},
            "acceptance": [{"type": "no_contract_violation"}],
        })
        with ExitStack() as stack:
            stack.enter_context(patch("pactflow.core.agent.get_provider", return_value=provider))
            stack.enter_context(patch("pactflow.core.agent.process_manager", self.manager))
            stack.enter_context(patch("pactflow.core.process.manager.load_active_contract", return_value=contract))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=contract))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.runtime_store", self.store))
            stack.enter_context(patch("pactflow.core.contracts.guard.load_active_contract", return_value=contract))
            stack.enter_context(patch("pactflow.core.contracts.report.write_report", return_value="report.json"))
            stack.enter_context(patch("pactflow.core.process.manager.write_report", return_value="report.json"))
            app = create_agent_app(tools=[])
            state = app.invoke(
                {"messages": [HumanMessage(content="first; second")], "summary": ""},
                config={"configurable": {"thread_id": "subtask-thread"}},
            )

        self.assertEqual(state["process_report"]["status"], "inconclusive")
        child_nodes = [node for node in self.store.list_task_nodes(state["run_id"]) if node["parent_node_id"]]
        self.assertEqual([node["status"] for node in child_nodes], ["waiting", "pending"])
        self.assertEqual(
            [event["event"] for event in self.store.get_run_events(state["run_id"])].count("task_completed"),
            0,
        )

    def test_forbidden_resource_is_not_written_and_fails_acceptance(self):
        provider = _provider_with_responses(
            _tool_call("write_office_file", {
                "filepath": "reports/blocked/result.txt",
                "content": "must not exist",
                "mode": "w",
            }),
            AIMessage(content="blocked"),
        )

        state = self._run_graph(
            provider,
            [write_office_file],
            self._write_contract(forbidden=["reports/blocked/**"]),
        )

        blocked_path = os.path.join(self.office_dir, "reports", "blocked", "result.txt")
        self.assertFalse(os.path.exists(blocked_path))
        self.assertEqual(state["process_report"]["status"], "failed")
        events = self.store.get_run_events(state["run_id"])
        self.assertIn("tool_denied", [event["event"] for event in events])

    def test_guarded_action_requires_and_consumes_one_time_approval(self):
        executions: list[str] = []

        @tool
        def external_action(value: str) -> str:
            """Execute a test-only external action."""
            executions.append(value)
            return f"executed:{value}"

        provider = _provider_with_responses(
            _tool_call("external_action", {"value": "same-args"}),
            AIMessage(content="completed"),
        )
        config = {"configurable": {"thread_id": "e2e-thread"}}
        with ExitStack() as stack:
            stack.enter_context(patch("pactflow.core.agent.get_provider", return_value=provider))
            stack.enter_context(patch("pactflow.core.agent.process_manager", self.manager))
            stack.enter_context(patch("pactflow.core.process.manager.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.runtime_store", self.store))
            stack.enter_context(patch("pactflow.core.contracts.guard.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.process.manager.write_report", return_value="report.json"))
            app = create_agent_app(tools=[external_action], checkpointer=MemorySaver())

            first = app.invoke(
                {"messages": [HumanMessage(content="execute exact action")], "summary": ""},
                config=config,
            )
            self.assertIn("__interrupt__", first)
            self.assertEqual(executions, [])
            self.assertEqual(provider.bind_tools.return_value.invoke.call_count, 1)

            action = self.store.list_pending_actions("e2e-thread")[0]
            original_args = action["args_json"]
            self.assertTrue(self.store.approve_pending_action(action["action_id"], "e2e-owner"))
            resumed = app.invoke(
                Command(resume={"action_id": action["action_id"], "status": "approved"}),
                config=config,
            )

        self.assertEqual(executions, ["same-args"])
        self.assertEqual(action["args_json"], original_args)
        self.assertEqual(provider.bind_tools.return_value.invoke.call_count, 2)
        self.assertEqual(resumed["execution_mode"], "guarded_action")
        self.assertEqual(resumed["process_report"]["status"], "passed")
        self.assertEqual(self.store.get_action(action["action_id"])["status"], "consumed")

    def test_rejected_action_resumes_without_execution(self):
        executions: list[str] = []

        @tool
        def external_action(value: str) -> str:
            """Execute a test-only external action."""
            executions.append(value)
            return f"executed:{value}"

        provider = _provider_with_responses(
            _tool_call("external_action", {"value": "blocked"}),
            AIMessage(content="rejected"),
        )
        config = {"configurable": {"thread_id": "reject-thread"}}
        with ExitStack() as stack:
            stack.enter_context(patch("pactflow.core.agent.get_provider", return_value=provider))
            stack.enter_context(patch("pactflow.core.agent.process_manager", self.manager))
            stack.enter_context(patch("pactflow.core.process.manager.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.runtime_store", self.store))
            stack.enter_context(patch("pactflow.core.contracts.guard.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.process.manager.write_report", return_value="report.json"))
            app = create_agent_app(tools=[external_action], checkpointer=MemorySaver())
            first = app.invoke(
                {"messages": [HumanMessage(content="reject action")], "summary": ""},
                config=config,
            )
            action = self.store.list_pending_actions("reject-thread")[0]
            self.assertTrue(self.store.reject_pending_action(action["action_id"], "e2e-owner"))
            resumed = app.invoke(
                Command(resume={"action_id": action["action_id"], "status": "rejected"}),
                config=config,
            )

        self.assertIn("__interrupt__", first)
        self.assertEqual(executions, [])
        self.assertEqual(self.store.get_action(action["action_id"])["status"], "rejected")
        self.assertEqual(resumed["process_report"]["status"], "failed")

    def test_approval_resumes_after_sqlite_checkpointer_reopen(self):
        executions: list[str] = []

        @tool
        def external_action(value: str) -> str:
            """Execute a persisted test action."""
            executions.append(value)
            return f"executed:{value}"

        provider = _provider_with_responses(
            _tool_call("external_action", {"value": "persisted-exact-args"}),
            AIMessage(content="completed after restart"),
        )
        config = {"configurable": {"thread_id": "restart-thread"}}
        checkpoint_path = os.path.join(self.temp_dir.name, "checkpoints.sqlite3")
        with ExitStack() as stack:
            stack.enter_context(patch("pactflow.core.agent.get_provider", return_value=provider))
            stack.enter_context(patch("pactflow.core.agent.process_manager", self.manager))
            stack.enter_context(patch("pactflow.core.process.manager.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.runtime_store", self.store))
            stack.enter_context(patch("pactflow.core.contracts.guard.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.process.manager.write_report", return_value="report.json"))

            with SqliteSaver.from_conn_string(checkpoint_path) as first_saver:
                first_app = create_agent_app(tools=[external_action], checkpointer=first_saver)
                interrupted = first_app.invoke(
                    {"messages": [HumanMessage(content="persist across restart")], "summary": ""},
                    config=config,
                )
                self.assertIn("__interrupt__", interrupted)
                snapshot = first_app.get_state(config)
                self.assertTrue(snapshot.tasks[0].interrupts)

            action = self.store.list_pending_actions("restart-thread")[0]
            self.assertTrue(self.store.approve_pending_action(action["action_id"], "e2e-owner"))

            with SqliteSaver.from_conn_string(checkpoint_path) as reopened_saver:
                reopened_app = create_agent_app(tools=[external_action], checkpointer=reopened_saver)
                restored = reopened_app.get_state(config)
                self.assertEqual(
                    restored.tasks[0].interrupts[0].value["action_id"],
                    action["action_id"],
                )
                resumed = reopened_app.invoke(
                    Command(resume={"action_id": action["action_id"], "status": "approved"}),
                    config=config,
                )

        self.assertEqual(executions, ["persisted-exact-args"])
        self.assertEqual(provider.bind_tools.return_value.invoke.call_count, 2)
        self.assertEqual(resumed["process_report"]["status"], "passed")
        self.assertEqual(self.store.get_action(action["action_id"])["status"], "consumed")

    def test_expired_approval_resumes_without_execution_and_is_audited(self):
        executions: list[str] = []

        @tool
        def external_action(value: str) -> str:
            """Execute a test action unless approval expires."""
            executions.append(value)
            return f"executed:{value}"

        provider = _provider_with_responses(
            _tool_call("external_action", {"value": "must-not-run"}),
            AIMessage(content="expired"),
        )
        config = {"configurable": {"thread_id": "expiry-thread"}}
        with ExitStack() as stack:
            stack.enter_context(patch("pactflow.core.agent.get_provider", return_value=provider))
            stack.enter_context(patch("pactflow.core.agent.process_manager", self.manager))
            stack.enter_context(patch("pactflow.core.process.manager.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.contracts.tool_node.runtime_store", self.store))
            stack.enter_context(patch("pactflow.core.contracts.guard.load_active_contract", return_value=None))
            stack.enter_context(patch("pactflow.core.process.manager.write_report", return_value="report.json"))
            app = create_agent_app(tools=[external_action], checkpointer=MemorySaver())
            app.invoke(
                {"messages": [HumanMessage(content="let approval expire")], "summary": ""},
                config=config,
            )
            action = self.store.list_pending_actions("expiry-thread")[0]
            with self.store._connect() as conn:
                conn.execute(
                    "UPDATE pending_actions SET expires_at = '2000-01-01T00:00:00Z' WHERE action_id = ?",
                    (action["action_id"],),
                )
            self.assertEqual(self.store.get_action(action["action_id"])["status"], "expired")
            resumed = app.invoke(
                Command(resume={"action_id": action["action_id"], "status": "expired"}),
                config=config,
            )

        self.assertEqual(executions, [])
        events = self.store.get_run_events(action["run_id"])
        self.assertIn("approval_expired", [event["event"] for event in events])
        self.assertIn("tool_denied", [event["event"] for event in events])
        self.assertEqual(resumed["process_report"]["status"], "failed")

    def test_sqlite_state_survives_store_reopen_and_isolates_runs(self):
        future = "2099-01-01 08:00:00"
        self.store.create_scheduled_task(future, "persisted", task_id="persist-1")
        first_run = self.store.start_run("thread", "first", "chat")
        second_run = self.store.start_run("thread", "second", "chat")
        self.store.append_run_event(first_run, "only_first")

        reopened = RuntimeStore(self.store.db_path, legacy_tasks_file=None)

        self.assertEqual(reopened.list_scheduled_tasks()[0]["id"], "persist-1")
        self.assertEqual([event["event"] for event in reopened.get_run_events(first_run)], ["only_first"])
        self.assertEqual(reopened.get_run_events(second_run), [])


if __name__ == "__main__":
    unittest.main()
