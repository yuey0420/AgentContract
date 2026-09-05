import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from pactflow.core.agent import create_agent_app
from pactflow.core.approval import ApprovalService
from pactflow.core.contracts.guard import guard_tool_call
from pactflow.core.contracts.models import TaskContract
from pactflow.core.contracts.store import approve_contract, compute_contract_hash
from pactflow.core.contracts.tool_node import ContractToolNode, _result_indicates_failure
from pactflow.core.process.evidence import capture_filesystem_snapshot, diff_filesystem_snapshots, reconcile_changes
from pactflow.core.process.manager import ProcessManager
from pactflow.core.process.task_planner import validate_model_plan, validate_task_graph
from pactflow.core.process.workspace import capture_recoverable_snapshot, restore_baseline_file, import_project_copy
from pactflow.core.process.acceptance_surface import acceptance_semantics_changed
from pactflow.core.runtime_store import RuntimeStore
from pactflow.core.tools.sandbox_tools import patch_office_file, run_office_check, write_office_file


def call(name, args, identifier):
    return {"name": name, "args": args, "id": identifier, "type": "tool_call"}


class TestProcessV3(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.office = self.root / "office"
        self.office.mkdir()
        self.store = RuntimeStore(str(self.root / "runtime.sqlite3"), legacy_tasks_file=None)
        self.manager = ProcessManager(self.store)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in ("pactflow.core.process.manager", "pactflow.core.contracts.report", "pactflow.core.tools.sandbox_tools"):
            self.stack.enter_context(patch(module + ".OFFICE_DIR", str(self.office)))
        self.stack.enter_context(patch("pactflow.core.contracts.store.CONTRACT_REPORTS_DIR", str(self.root / "reports")))
        self.stack.enter_context(patch("pactflow.core.contracts.tool_node.runtime_store", self.store))
        self.stack.enter_context(patch("pactflow.core.contracts.guard.runtime_store", self.store))
        self.stack.enter_context(patch("pactflow.core.agent.process_manager", self.manager))
        self.contract = approve_contract(TaskContract.model_validate({
            "contract_version": "2.0", "id": "v3-test", "owner": "tester", "objective": "fix report",
            "inputs": {"planner_mode": "deterministic", "acceptance_required": False},
            "scope": {"can_read": ["**"], "can_write": ["**"], "can_execute": ["check:content"]},
            "tool_policy": {"allowed_tools": ["write_office_file", "patch_office_file", "run_office_check"]},
            "acceptance": [{"id": "AC-BASE", "type": "tool_called", "tool": "write_office_file"}],
        }), "tester")
        for module in ("pactflow.core.process.manager", "pactflow.core.contracts.tool_node", "pactflow.core.contracts.guard"):
            self.stack.enter_context(patch(module + ".load_active_contract", side_effect=lambda: self.contract))
        self.config = {"configurable": {"thread_id": "test-thread"}}

    def start(self):
        return self.manager.start("test-thread", "fix report")

    def resign(self):
        self.contract = approve_contract(self.contract, "tester")

    def test_deny_wins_over_review_and_human_confirmation(self):
        self.contract.scope.review_required = ["**"]
        self.contract.scope.human_only = ["**"]
        self.contract.scope.cannot_write = ["blocked.txt"]
        self.resign()
        decision = guard_tool_call("test-thread", "write_office_file", {"filepath": "blocked.txt"}, capability="write", resource="blocked.txt")
        self.assertEqual(decision.decision, "deny")
        self.assertIn("scope.cannot_write", decision.matched_clauses)

    def test_batch_reserves_file_budget_before_execution(self):
        self.contract.limits.max_written_files = 1
        self.resign()
        state = self.start()
        state["messages"] = [AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "a.txt", "content": "a"}, "a"), call("write_office_file", {"filepath": "b.txt", "content": "b"}, "b")])]
        ContractToolNode([write_office_file])(state, self.config)
        self.assertTrue((self.office / "a.txt").exists())
        self.assertFalse((self.office / "b.txt").exists())

    def test_confirmation_cannot_override_zero_budget(self):
        self.contract.scope.review_required = ["**"]
        self.contract.limits.max_tool_calls = 0
        self.resign()
        state = self.start()
        state["messages"] = [AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "a", "content": "x"}, "a")])]
        with patch("pactflow.core.contracts.tool_node.interrupt") as prompt:
            ContractToolNode([write_office_file])(state, self.config)
        prompt.assert_not_called()
        self.assertFalse((self.office / "a").exists())

    def test_batch_second_interrupt_and_replay_do_not_repeat_side_effects(self):
        self.contract.scope.review_required = ["**"]
        self.resign()
        state = self.start()
        state["messages"] = [AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": name, "content": name, "mode": "a"}, name) for name in ("a", "b")])]
        node = ContractToolNode([write_office_file])
        paused = True

        def approve(payload):
            if payload["arguments"]["filepath"] == "b" and paused:
                raise RuntimeError("pause second action")
            self.store.approve_pending_action(payload["action_id"], "tester")
            return {"action_id": payload["action_id"]}

        with patch("pactflow.core.contracts.tool_node.interrupt", side_effect=approve):
            with self.assertRaisesRegex(RuntimeError, "pause second"):
                node(state, self.config)
            self.assertFalse((self.office / "a").exists())
            reopened = RuntimeStore(self.store.db_path, legacy_tasks_file=None)
            self.assertEqual(len(reopened.list_run_actions(state["run_id"])), 2)
            paused = False
            result = node(state, self.config)
            replayed = node(state, self.config)
        self.assertEqual([message.content for message in result["messages"]], [message.content for message in replayed["messages"]])
        self.assertEqual((self.office / "a").read_text().strip(), "a")
        self.assertEqual(sum(e["event"] == "approval_consumed" for e in self.store.get_run_events(state["run_id"])), 2)

    def test_started_action_becomes_unknown_without_execution(self):
        state = self.start()
        args = {"filepath": "never", "content": "x"}
        self.store.register_execution(state["run_id"], "a", "write_office_file", args, compute_contract_hash(self.contract), "write", "never")
        self.store.authorize_execution(state["run_id"], "a")
        self.store.start_execution(state["run_id"], "a")
        state["messages"] = [AIMessage(content="", tool_calls=[call("write_office_file", args, "a")])]
        ContractToolNode([write_office_file])(state, self.config)
        self.assertFalse((self.office / "never").exists())
        self.assertEqual(self.store.get_process_run(state["run_id"])["waiting"]["reason"], "outcome_unknown")

    def test_reused_call_id_with_changed_arguments_is_rejected(self):
        run = self.start()["run_id"]
        self.store.register_execution(run, "a", "write_office_file", {"content": "a"}, "hash", "write", "a")
        with self.assertRaisesRegex(ValueError, "changed arguments"):
            self.store.register_execution(run, "a", "write_office_file", {"content": "b"}, "hash", "write", "a")

    def test_scope_never_expands_interpreter_or_parent_directory(self):
        self.assertEqual(ApprovalService._resource_pattern("python first.py", "development"), "python first.py")
        self.assertEqual(ApprovalService._resource_pattern("docs/a.md", "development"), "docs/a.md")
        self.assertFalse(ApprovalService.scope_eligible({"contract_hash": "hash", "risk_level": "low", "process_profile": "development", "capability": "execute", "clause": "scope.review_required"}))

    def test_nonzero_exit_is_failure_even_when_stdout_says_success(self):
        self.assertTrue(_result_indicates_failure(json.dumps({"status": "succeeded", "exit_code": 1, "stdout": "all good"})))
        self.assertTrue(_result_indicates_failure("Success! Exit Code: 1"))

    def test_model_claim_has_no_completion_evidence(self):
        state = self.start()
        self.manager.complete_active_task(state["run_id"], {"response": "done"})
        self.assertEqual(self.store.list_task_nodes(state["run_id"])[0]["status"], "ready_for_verification")
        report = self.manager.finalize(state["run_id"], "test-thread")
        self.assertNotEqual(report["task_plan"][0]["status"], "completed")

    def test_run_node_identity_preserves_both_runs(self):
        first, second = self.start(), self.start()
        a = self.store.list_task_nodes(first["run_id"])[0]
        b = self.store.list_task_nodes(second["run_id"])[0]
        self.assertEqual(a["logical_node_id"], b["logical_node_id"])
        self.assertNotEqual(a["node_id"], b["node_id"])
        self.store.update_task_node(a["node_id"], status="waiting")
        self.assertEqual(self.store.list_task_nodes(second["run_id"])[0]["status"], "running")

    def test_invalid_dependencies_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_model_plan({"tasks": [{"id": "a", "objective": "x", "depends_on": ["a"]}]}, "root", max_children=3)
        for dependencies in ((["b"], ["a"]), (["missing"], []), (["a"], [])):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                validate_task_graph([{"node_id": "a", "depends_on": dependencies[0]}, {"node_id": "b", "depends_on": dependencies[1]}])

    def test_plan_revision_keeps_contract_approval_valid(self):
        state = self.start()
        original = compute_contract_hash(self.contract)
        plan = self.store.get_record("plan", state["run_id"])
        self.manager.revise_plan(state["run_id"], plan["payload"]["steps"], expected_revision=1, predicted_files=["new-helper.py"])
        self.assertEqual(original, compute_contract_hash(self.contract))
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.manager.revise_plan(state["run_id"], plan["payload"]["steps"], expected_revision=1)

    def test_snapshot_change_prevents_write(self):
        state = self.start()
        self.contract.scope.can_write = ["elsewhere/**"]
        self.resign()
        state["messages"] = [AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "a", "content": "x"}, "a")])]
        ContractToolNode([write_office_file])(state, self.config)
        self.assertFalse((self.office / "a").exists())
        self.assertEqual(self.manager.finalize(state["run_id"], "test-thread")["status"], "inconclusive")

    def test_snapshot_truncation_cannot_pass_reconciliation(self):
        (self.office / "a").write_text("a")
        (self.office / "b").write_text("b")
        limited = capture_filesystem_snapshot(str(self.office), max_files=1)
        self.assertEqual(limited["coverage"], "limited")
        report = reconcile_changes(diff_filesystem_snapshots(limited, limited), ["**"])
        self.assertEqual(report["status"], "needs_review")

    def test_patch_conflict_preserves_human_edit(self):
        target = self.office / "file.txt"
        target.write_text("human edit")
        result = patch_office_file.invoke({"filepath": "file.txt", "expected_content_hash": hashlib.sha256(b"old").hexdigest(), "content": "agent edit"})
        self.assertEqual(result["reason"], "content_conflict")
        self.assertEqual(target.read_text(), "human edit")

    def test_decision_does_not_grant_permissions_and_checks_revision(self):
        run = self.start()["run_id"]
        payload = {"kind": "product_choice", "question": "retain?", "options": [{"id": "yes", "label": "retain"}], "authorization_effect": "none"}
        request = self.store.request_decision(run, "product", payload)
        self.assertEqual(request, self.store.request_decision(run, "product", payload))
        with self.assertRaises(ValueError):
            self.store.resolve_decision(request, 2, "tester", "yes")
        result = self.store.resolve_decision(request, 1, "tester", "yes")
        self.assertEqual(result["authorization_effect"], "none")
        self.assertEqual(self.store.list_approval_grants(run), [])

    def test_waiting_controls_preserve_work_item(self):
        run = self.start()["run_id"]
        self.assertEqual(self.manager.handle_control(run, "/status")["kind"], "status_query")
        original = self.store.get_process_run(run)["work_item_id"]
        feedback = self.manager.handle_control(run, "keep existing error message")
        self.assertEqual(feedback["kind"], "feedback")
        self.assertEqual(self.store.get_process_run(run)["work_item_id"], original)
        self.manager.handle_control(run, "/cancel")
        self.assertEqual(self.store.get_process_run(run)["terminal_result"], "cancelled")
        self.assertFalse(self.store.transition_run(run, "executing"))

    def test_repair_budget_survives_reopen(self):
        run = self.start()["run_id"]
        self.assertTrue(self.store.reserve_repair(run, {}))
        reopened = RuntimeStore(self.store.db_path, legacy_tasks_file=None)
        self.assertEqual(reopened.get_process_run(run)["repair_count"], 1)
        self.assertTrue(reopened.reserve_repair(run, {}))
        self.assertTrue(reopened.reserve_repair(run, {}))
        self.assertFalse(reopened.reserve_repair(run, {}))

    def test_hypothesis_cannot_replace_confirmed_context(self):
        confirmed = {"project_id": "project", "entries": [{"text": "offline only", "status": "confirmed", "sources": ["user"], "confirmed_by": "tester"}]}
        self.manager.revise_context(confirmed, expected_revision=0, actor="tester")
        with self.assertRaises(ValueError):
            self.manager.revise_context(confirmed, expected_revision=1, actor="agent")
        self.assertEqual(self.store.get_record("product_context", "project")["revision"], 1)
        self.manager.revise_context({"project_id": "project", "entries": [{"text": "use online service", "status": "hypothesis"}]}, expected_revision=1, actor="agent")
        self.assertEqual(self.store.get_record("product_context", "project")["payload"]["entries"][0]["text"], "offline only")

    def test_assertion_removal_requires_review_but_new_assertions_do_not(self):
        before = "def test_result():\n    assert 1 == 1\n"
        self.assertTrue(acceptance_semantics_changed("tests/test_result.py", before, "def test_result():\n    pass\n"))
        self.assertFalse(acceptance_semantics_changed("tests/test_result.py", before, before + "    assert 2 == 2\n"))
        self.assertTrue(acceptance_semantics_changed("tests/test_result.py", before, "import pytest\n@pytest.mark.skip\n" + before))

    def test_test_weakening_is_intercepted_by_gateway(self):
        (self.office / "test_result.py").write_text("assert False\n")
        state = self.start()
        state["messages"] = [AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "test_result.py", "content": "pass\n"}, "weaken")])]
        with patch("pactflow.core.contracts.tool_node.interrupt", side_effect=RuntimeError("review")):
            with self.assertRaisesRegex(RuntimeError, "review"):
                ContractToolNode([write_office_file])(state, self.config)
        self.assertEqual((self.office / "test_result.py").read_text(), "assert False\n")
        self.assertEqual(self.store.list_pending_actions()[0]["clause"], "acceptance.semantic_change")

    def test_content_baseline_restores_only_expected_revision(self):
        target = self.office / "source.txt"
        target.write_bytes(b"baseline")
        snapshot = capture_recoverable_snapshot(str(self.office), self.root / "objects")
        target.write_bytes(b"human")
        with self.assertRaisesRegex(ValueError, "content_conflict"):
            restore_baseline_file(snapshot, "source.txt", self.root / "objects", hashlib.sha256(b"stale").hexdigest())
        self.assertEqual(target.read_bytes(), b"human")
        restore_baseline_file(snapshot, "source.txt", self.root / "objects", hashlib.sha256(b"human").hexdigest())
        self.assertEqual(target.read_bytes(), b"baseline")

    def test_external_project_is_copied_inside_office(self):
        source = self.root / "original"
        source.mkdir()
        (source / "code.py").write_text("original")
        binding = import_project_copy(self.store, str(source), str(self.office), "demo")
        copied = Path(binding["root"]) / "code.py"
        self.assertEqual(copied.read_text(), "original")
        copied.write_text("changed")
        self.assertEqual((source / "code.py").read_text(), "original")
        with self.assertRaises(ValueError):
            import_project_copy(self.store, str(source), str(self.office), "../escape")

    def test_scoped_literal_grant_consumption_is_idempotent(self):
        self.contract.scope.review_required = ["**"]
        self.resign()
        state = self.start()
        grant = self.store.create_approval_grant(run_id=state["run_id"], thread_id="test-thread", contract_hash=compute_contract_hash(self.contract),
                                               tool_name="write_office_file", capability="write", resource_pattern="a.txt", approved_by="tester")
        state["messages"] = [AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "a.txt", "content": "x"}, "scoped")])]
        with patch("pactflow.core.contracts.tool_node.interrupt") as prompt:
            node = ContractToolNode([write_office_file])
            node(state, self.config)
            node(state, self.config)
        prompt.assert_not_called()
        self.assertEqual(self.store.get_approval_grant(grant["grant_id"])["uses"], 1)

    def test_human_acceptance_is_separate_from_technical_delivery(self):
        self.contract.inputs["acceptance_required"] = True
        self.resign()
        state = self.start()
        self.store.append_run_event(state["run_id"], "tool_succeeded", {"tool": "write_office_file"})
        report = self.manager.finalize(state["run_id"], "test-thread")
        self.assertEqual(report["delivery"]["human_acceptance"], "pending")
        request = self.store.list_decisions(state["run_id"])[0]
        self.manager.resolve_decision(request["request_id"], 1, "tester", "accepted")
        self.assertEqual(self.manager.prepare_resume(state["run_id"])["human_acceptance"]["choice"], "accepted")
        self.assertEqual(report["delivery"]["human_acceptance"], "pending")

    def test_graph_failure_repairs_and_report_is_atomic_complete(self):
        self.contract.inputs.update({"checks": {"content": {"argv": [sys.executable, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('result.txt').read_text() == 'good' else 1)"], "version": "1"}},
                                     "scenarios": [{"id": "SC-1", "description": "correct result", "check_id": "content"}]})
        self.resign()
        responses = []
        for index, content in enumerate(("bad", "good")):
            responses.extend([AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "result.txt", "content": content}, f"write-{index}")]),
                              AIMessage(content="", tool_calls=[call("run_office_check", {"check_id": "content"}, f"check-{index}")]), AIMessage(content="ready")])
        provider = Mock()
        provider.bind_tools.return_value.invoke.side_effect = responses
        with patch("pactflow.core.agent.get_provider", return_value=provider):
            app = create_agent_app(tools=[write_office_file, run_office_check])
            state = app.invoke({"messages": [HumanMessage(content="fix report")]}, self.config)
        report = state["process_report"]
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["delivery"]["human_acceptance"], "not_requested")
        self.assertEqual(self.store.get_process_run(state["run_id"])["repair_count"], 1)
        self.assertEqual(report["summary"]["historical_failures"], 1)
        saved = json.loads(next((self.root / "reports").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved, report)
        self.assertEqual(saved["terminal_result"], "succeeded")

    def test_product_decision_graph_waits_for_persisted_resolution(self):
        self.contract = None
        provider = Mock()
        provider.bind_tools.return_value.invoke.side_effect = [AIMessage(content="", tool_calls=[call("request_product_decision", {"question": "retain?", "options": [{"id": "retain", "label": "Retain"}]}, "choice")]), AIMessage(content="done")]
        with patch("pactflow.core.agent.get_provider", return_value=provider):
            app = create_agent_app(tools=[], checkpointer=MemorySaver())
            first = app.invoke({"messages": [HumanMessage(content="discuss behavior")]}, self.config)
            decision = first["__interrupt__"][0].value
            self.manager.resolve_decision(decision["request_id"], 1, "tester", "retain")
            final = app.invoke(Command(resume={"request_id": decision["request_id"]}), self.config)
        self.assertEqual(final["terminal_result"], "succeeded")
        self.assertEqual(self.store.list_run_actions(final["run_id"]), [])

    def test_verifier_runs_required_check_without_model_tool_call(self):
        self.contract.inputs.update({"checks": {"content": {"argv": [sys.executable, "-c", "print('verified')"]}},
                                     "scenarios": [{"id": "SC-1", "description": "check process", "check_id": "content"}]})
        self.contract.acceptance = []
        self.resign()
        provider = Mock()
        provider.bind_tools.return_value.invoke.return_value = AIMessage(content="ready")
        with patch("pactflow.core.agent.get_provider", return_value=provider):
            app = create_agent_app(tools=[run_office_check])
            result = app.invoke({"messages": [HumanMessage(content="verify")]}, self.config)
        self.assertEqual(result["process_report"]["status"], "passed")
        self.assertEqual(provider.bind_tools.return_value.invoke.call_count, 1)
        self.assertTrue(any(event.get("tool") == "run_office_check" and event["event"] == "tool_succeeded" for event in self.store.get_run_events(result["run_id"])))

    def test_new_source_content_invalidates_waiting_approval(self):
        from langchain_core.messages import ToolMessage
        self.contract.scope.review_required = ["**"]
        self.resign()
        state = self.start()
        state["messages"] = [ToolMessage(content="source one", tool_call_id="source", name="read_office_file", additional_kwargs={"security": {"trustworthiness": "untrusted"}}),
                             AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "a", "content": "source one"}, "write")])]
        with patch("pactflow.core.contracts.tool_node.interrupt", side_effect=RuntimeError("waiting")):
            with self.assertRaises(RuntimeError):
                ContractToolNode([write_office_file])(state, self.config)
        action = self.store.list_pending_actions()[0]
        self.store.approve_pending_action(action["action_id"], "tester")
        state["messages"][0].content = "changed source"
        with patch("pactflow.core.contracts.tool_node.interrupt") as prompt:
            ContractToolNode([write_office_file])(state, self.config)
        prompt.assert_not_called()
        self.assertFalse((self.office / "a").exists())

    def test_second_run_cannot_write_while_first_outcome_is_unknown(self):
        first, second = self.start(), self.start()
        for state in (first, second):
            self.store.register_execution(state["run_id"], "write", "write_office_file", {}, "hash", "write", "a")
            self.store.authorize_execution(state["run_id"], "write")
        self.assertTrue(self.store.start_execution(first["run_id"], "write"))
        self.assertFalse(self.store.start_execution(second["run_id"], "write"))
        self.store.finish_execution(first["run_id"], "write", "outcome_unknown", {"content": "unknown"})
        self.assertFalse(self.store.start_execution(second["run_id"], "write"))

    def test_scope_approval_repeated_click_does_not_mint_more_grants(self):
        run = self.start()["run_id"]
        action = self.store.create_pending_action(run, "test-thread", "write_office_file", {"filepath": "a", "content": "x"},
                    compute_contract_hash(self.contract), tool_call_id="write", risk_level="medium", clause="scope.review_required",
                    capability="write", resource="a", process_profile="development", effect="write")
        service = ApprovalService(self.store)
        service.approve_scope(action, "tester")
        service.approve_scope(action, "tester")
        self.assertEqual(len(self.store.list_approval_grants(run)), 1)

    def test_scoped_approval_resume_consumes_original_action_and_finishes(self):
        self.contract.scope.review_required = ["**"]
        self.resign()
        provider = Mock()
        provider.bind_tools.return_value.invoke.side_effect = [
            AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "a.txt", "content": "first"}, "first")]),
            AIMessage(content="", tool_calls=[call("write_office_file", {"filepath": "a.txt", "content": "second"}, "second")]),
            AIMessage(content="ready"),
        ]
        with patch("pactflow.core.agent.get_provider", return_value=provider):
            app = create_agent_app(tools=[write_office_file], checkpointer=MemorySaver())
            paused = app.invoke({"messages": [HumanMessage(content="write file")]}, self.config)
            request = paused["__interrupt__"][0].value
            ApprovalService(self.store).approve_scope(request["action_id"], "tester")
            final = app.invoke(Command(resume={"action_id": request["action_id"]}), self.config)
        self.assertEqual(final["process_report"]["status"], "passed")
        self.assertEqual(self.store.get_action(request["action_id"])["status"], "consumed")
        self.assertEqual(final["process_report"]["summary"]["pending_approvals"], 0)
        self.assertEqual(final["process_report"]["summary"]["approval_prompts"], 1)
        self.assertEqual(final["process_report"]["summary"]["approval_grant_reuses"], 1)
        self.assertEqual((self.office / "a.txt").read_text(), "second")

    def test_cli_commands_and_draft_example_are_valid(self):
        from typer.testing import CliRunner
        from entry.cli import app
        result = CliRunner().invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for command in ("run-status", "decisions", "resolve-decision", "workspace-import", "baseline-restore"):
            self.assertIn(command, result.output)
        contract_path = Path(__file__).resolve().parents[1] / "examples/contracts/v3-scenario.contract.json"
        draft = TaskContract.model_validate_json(contract_path.read_text(encoding="utf-8"))
        self.assertEqual(draft.status, "draft")
        self.assertIsNone(draft.approval)

    def test_legacy_node_migration_preserves_dependencies_and_identifiers(self):
        import sqlite3
        database = self.root / "legacy.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE task_nodes (node_id TEXT PRIMARY KEY, run_id TEXT, parent_node_id TEXT, depends_on_json TEXT)")
            connection.executemany("INSERT INTO task_nodes VALUES (?, ?, ?, ?)", [("root", "old", None, "[]"), ("a", "old", "root", "[]"), ("b", "old", "root", '["a"]')])
        # Exercise only V3 migration against the historic identity columns.
        store = object.__new__(RuntimeStore)
        store.db_path = str(database)
        with store._connect() as connection:
            connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
            connection.execute("CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("CREATE TABLE approval_grants (grant_id TEXT PRIMARY KEY)")
        store.initialize_process_storage()
        store.initialize_process_storage()
        with store._connect() as connection:
            row = connection.execute("SELECT * FROM task_nodes WHERE node_id='old:b'").fetchone()
        self.assertEqual(row["logical_node_id"], "b")
        self.assertEqual(row["parent_node_id"], "old:root")
        self.assertEqual(json.loads(row["depends_on_json"]), ["old:a"])
