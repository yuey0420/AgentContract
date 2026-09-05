from typing import Any
from pathlib import Path
import uuid

from ..contracts.report import approval_digest, generate_contract_report
from ..contracts.store import compute_contract_hash, load_active_contract, write_report
from ..config import OFFICE_DIR
from ..logger import audit_logger
from ..runtime_store import RuntimeStore, runtime_store
from .capabilities import assess_contract_capabilities
from .evidence import capture_filesystem_snapshot, diff_filesystem_snapshots, reconcile_changes
from .task_planner import decompose_objective
from .models import WorkItem, PlanRevision, Delivery, DecisionRequest, ProductContext
from .verification import evaluate_scenarios, aggregate
from ..contracts.models import TaskContract
from .workspace import capture_recoverable_snapshot, capture_git_baseline


class ProcessManager:
    def __init__(self, store: RuntimeStore = runtime_store, planner=decompose_objective):
        self.store = store
        self.planner = planner

    def active_contract(self):
        try:
            return load_active_contract()
        except Exception:
            return None

    @staticmethod
    def normalize_process_profile(profile: str | None) -> str:
        aliases = {"managed": "development", "federated": "audited"}
        normalized = aliases.get(str(profile or "development"), str(profile or "development"))
        return normalized if normalized in {"chat", "development", "audited"} else "development"

    @staticmethod
    def resolve_planner_mode(contract) -> str:
        """Select planning from the user-facing process profile.

        Explicit planner_mode remains an advanced override. Legacy 0.x
        contracts stay deterministic unless they explicitly opt into a richer
        profile, while current managed profiles use the model planner.
        """
        if contract is None:
            return "none"
        configured = contract.inputs.get("planner_mode")
        if configured in {"model", "deterministic", "none"}:
            return str(configured)
        raw_profile = str(contract.inputs.get("process_profile", contract.process_profile))
        profile = ProcessManager.normalize_process_profile(raw_profile)
        if profile == "chat":
            return "none"
        if str(contract.contract_version).startswith("0.") and raw_profile == "managed":
            return "deterministic"
        return "model"

    def start(
        self,
        thread_id: str,
        objective: str,
        *,
        acknowledged_by: str = "runtime",
        planner=None,
    ) -> dict[str, Any]:
        contract = None
        try:
            contract = load_active_contract()
        except Exception:
            contract = None

        contract_hash = compute_contract_hash(contract) if contract else None
        mode = "managed_task" if contract else "chat"
        process_profile = (
            self.normalize_process_profile(
                str(contract.inputs.get("process_profile", contract.process_profile))
            )
            if contract else "chat"
        )
        effective_policy = assess_contract_capabilities(contract) if contract else None
        planner_mode = self.resolve_planner_mode(contract)
        task_id = (
            contract.task_id if contract and contract.task_id
            else (contract.id if contract else None)
        )
        run_id = self.store.start_run(
            thread_id,
            objective,
            mode,
            contract.id if contract else None,
            contract_hash,
            task_id=task_id,
            task_version=contract.task_version if contract else None,
            plan_version=contract.plan_version if contract else None,
            policy_version=contract.policy_version if contract else None,
            effective_policy=effective_policy,
        )
        work_item_id = str(contract.inputs.get("work_item_id") or uuid.uuid4().hex) if contract else uuid.uuid4().hex
        existing_work = self.store.get_record("work_item", work_item_id)
        work = WorkItem(work_item_id=work_item_id,
                        project_id=str(contract.inputs.get("project_id", "local")) if contract else "local",
                        objective=objective, owner=contract.owner if contract else "local_user",
                        non_goals=contract.inputs.get("non_goals", []) if contract else [],
                        scenarios=contract.inputs.get("scenarios", []) if contract else [],
                        acceptance_required=bool(contract.inputs.get("acceptance_required", True)) if contract else False,
                        workspace_root=str(Path(OFFICE_DIR).resolve()))
        if not existing_work or existing_work["payload"] != work.model_dump():
            self.store.revise_record("work_item", work_item_id, work.model_dump(), expected_revision=existing_work["revision"] if existing_work else 0, actor="user_intent", run_id=run_id)
        self.store.bind_process_run(run_id, work_item_id, contract.model_dump() if contract else None,
                                    str(Path(OFFICE_DIR).resolve()),
                                    max_repairs=int(contract.inputs.get("max_repairs", 3)) if contract else 0)
        work_revision = self.store.get_record("work_item", work_item_id)["revision"]
        context = self.store.get_record("product_context", work.project_id)
        with self.store._connect() as conn:
            conn.execute("UPDATE process_runs SET work_revision=?, context_revision=? WHERE run_id=?", (work_revision, context["revision"] if context else 0, run_id))
        self.store.append_run_event(run_id, "run_started", {
            "mode": mode,
            "contract_id": contract.id if contract else None,
            "contract_hash": contract_hash,
            "process_profile": process_profile,
        })
        task_plan: list[dict[str, Any]] = []
        repositories: list[dict[str, Any]] = []
        orchestrate_subtasks = False
        planner_metadata: dict[str, Any] = {"mode": planner_mode}
        if contract and task_id:
            auto_decompose = bool(contract.inputs.get("auto_decompose", contract.auto_decompose))
            orchestrate_subtasks = bool(
                contract.inputs.get("orchestrate_subtasks", contract.orchestrate_subtasks)
            )
            max_subtasks = int(contract.inputs.get("max_subtasks", contract.max_subtasks))
            if auto_decompose and planner_mode != "none":
                try:
                    if planner_mode == "model" and planner is not None:
                        planned = planner(objective, task_id, max_subtasks, contract)
                        if isinstance(planned, tuple):
                            task_plan, planner_metadata = planned
                        else:
                            task_plan = planned
                    else:
                        task_plan = self.planner(
                            objective,
                            task_id,
                            max_children=max_subtasks,
                            contract=contract,
                        )
                        planner_metadata = {"mode": "deterministic"}
                except Exception as error:
                    task_plan = self.planner(objective, task_id, max_children=max_subtasks, contract=contract)
                    planner_metadata = {
                        "mode": "deterministic_fallback",
                        "error": str(error)[:300],
                    }
                    self.store.append_run_event(run_id, "task_plan_fallback", planner_metadata)
            else:
                task_plan = self.planner(objective, task_id, max_children=1, contract=contract)[:1]
                planner_metadata = {"mode": "none" if planner_mode == "none" else "deterministic"}
            self.store.create_task_nodes(run_id, task_plan)
            self.revise_plan(run_id, task_plan, expected_revision=0)
            self.store.append_run_event(run_id, "task_plan_created", {
                "task_id": task_id,
                "node_count": len(task_plan),
                "auto_decompose": auto_decompose,
                "orchestrate_subtasks": orchestrate_subtasks,
                "planner": planner_metadata,
            })
            if orchestrate_subtasks:
                self.activate_next_task(run_id)
            repositories = contract.inputs.get("repositories", contract.repositories) or []
            for repository in repositories:
                if not isinstance(repository, dict) or not repository.get("path"):
                    continue
                self.store.register_repository(
                    run_id,
                    str(repository.get("name") or repository["path"]),
                    str(repository["path"]),
                    role=str(repository.get("role") or "primary"),
                    revision=repository.get("revision"),
                    dependency=repository.get("dependency") or {},
                )
            audited = process_profile in {"development", "audited"}
            if audited:
                object_dir = Path(self.store.db_path).parent / "content_objects"
                snapshot = capture_recoverable_snapshot(OFFICE_DIR, object_dir)
                if contract.inputs.get("git_baseline"):
                    snapshot["git"] = capture_git_baseline(OFFICE_DIR)
                self.store.create_baseline(
                    run_id, snapshot, root_path=snapshot["root"], backend="filesystem"
                )
                self.store.create_checkpoint(
                    run_id,
                    "baseline",
                    "recorded",
                    "tool_generated",
                    {"file_count": len(snapshot.get("files", {})), "truncated": snapshot.get("truncated", False)},
                )
        self.store.append_run_event(run_id, "plan_created", {
            "task_id": contract.task_id if contract else None,
            "task_version": contract.task_version if contract else None,
            "plan_version": contract.plan_version if contract else None,
            "policy_version": contract.policy_version if contract else None,
        })
        # Existing graph callers do not have a separate plan-confirmation UI.
        # Record that runtime gate explicitly while keeping execution compatible.
        self.store.acknowledge_plan(
            run_id,
            acknowledged_by=acknowledged_by,
            plan_version=contract.plan_version if contract else None,
        )
        self.store.append_run_event(run_id, "plan.runtime_registered" if acknowledged_by == "runtime" else "plan.human_acknowledged", {"actor": acknowledged_by, "authorization_source": contract_hash})
        self.store.transition_run(run_id, "executing")
        if contract:
            self.store.create_checkpoint(
                run_id,
                "plan_acknowledged",
                "passed",
                "tool_generated",
                {"task_count": len(task_plan), "process_profile": process_profile},
            )
        audit_logger.log_event(
            thread_id=thread_id,
            event="process_started",
            run_id=run_id,
            mode=mode,
            contract_id=contract.id if contract else None,
        )
        return {
            "run_id": run_id,
            "work_item_id": work_item_id,
            "run_status": "executing",
            "terminal_result": None,
            "execution_mode": mode,
            "process_phase": "execute",
            "lifecycle_status": "executing",
            "task_id": task_id,
            "task_version": contract.task_version if contract else None,
            "plan_version": contract.plan_version if contract else None,
            "policy_version": contract.policy_version if contract else None,
            "process_profile": process_profile,
            "task_plan": self.store.list_task_nodes(run_id),
            "effective_policy": effective_policy or {},
            "repositories": repositories,
            "orchestrate_subtasks": orchestrate_subtasks,
            "planner_mode": planner_mode,
            "planner_metadata": planner_metadata,
            "process_report": {},
        }

    def acknowledge_plan(
        self,
        run_id: str,
        acknowledged_by: str = "operator",
        plan_version: str | None = None,
    ) -> bool:
        return self.store.acknowledge_plan(run_id, acknowledged_by, plan_version)

    def decide_acceptance(
        self,
        run_id: str,
        decision: str,
        decided_by: str = "operator",
        reason: str = "",
    ) -> bool:
        return self.store.decide_acceptance(run_id, decision, decided_by, reason)

    def close(
        self,
        run_id: str,
        closed_by: str = "operator",
        result: str = "accepted",
    ) -> bool:
        return self.store.close_run(run_id, closed_by, result)

    def activate_next_task(self, run_id: str) -> dict[str, Any] | None:
        nodes = self.store.list_task_nodes(run_id)
        parents = {node.get("parent_node_id") for node in nodes}
        leaves = [node for node in nodes if node["node_id"] not in parents]
        running = next((node for node in leaves if node.get("status") == "running"), None)
        if running:
            return running
        completed = {node["node_id"] for node in leaves if node.get("status") == "completed"}
        for node in leaves:
            if node.get("status") != "pending":
                continue
            if all(dependency in completed for dependency in node.get("depends_on", [])):
                self.store.update_task_node(node["node_id"], status="running")
                self.store.append_run_event(run_id, "task_started", {
                    "node_id": node["node_id"],
                    "objective": node.get("objective"),
                })
                return next(
                    item for item in self.store.list_task_nodes(run_id)
                    if item["node_id"] == node["node_id"]
                )
        if any(node["status"] == "pending" for node in leaves) and not any(node["status"] in {"running", "ready_for_verification", "verifying", "waiting"} for node in leaves):
            self.store.transition_run(run_id, "waiting", waiting={"reason": "dependency_deadlock", "blocked_node_ids": [node["node_id"] for node in leaves if node["status"] == "pending"], "resume_target": "executing"})
        return None

    def complete_active_task(self, run_id: str, output: dict[str, Any] | None = None) -> dict[str, Any] | None:
        nodes = self.store.list_task_nodes(run_id)
        current = next(
            (node for node in nodes if node.get("status") == "running"),
            None,
        )
        if not current:
            return self.activate_next_task(run_id)
        self.store.update_task_node(current["node_id"], status="ready_for_verification", output=output or {})
        self.store.append_run_event(run_id, "task_ready_for_verification", {
            "node_id": current["node_id"],
            "output": output or {},
        })
        return current

    def evaluate_run(self, run_id: str, thread_id: str) -> dict[str, Any]:
        previous = self.store.get_process_run(run_id)
        stopped = previous and (previous["run_status"] == "finished" or (previous.get("waiting") or {}).get("reason") in {"user_pause", "feedback_requires_revision", "outcome_unknown"})
        if not stopped:
            self.store.transition_run(run_id, "verifying")
        run = self.store.get_run(run_id)
        process = self.store.get_process_run(run_id)
        contract = TaskContract.model_validate(process["contract"]) if process and process.get("contract") else None
        snapshot_changed = False
        try:
            candidate = load_active_contract()
            snapshot_changed = (compute_contract_hash(candidate) if candidate else None) != (run or {}).get("contract_hash")
        except Exception:
            snapshot_changed = True

        audit_logger.flush()
        if contract:
            report = generate_contract_report(contract, thread_id=thread_id, run_id=run_id, store=self.store, persist=False)
        else:
            events = self.store.get_run_events(run_id)
            failed = [event for event in events if event.get("event") in {"tool_failed", "tool_denied"}]
            run_actions = self.store.list_run_actions(run_id)
            pending = [
                action for action in run_actions
                if action.get("status") in {"pending", "approved"}
            ]
            if not run_actions:
                pending = [event for event in events if event.get("event") == "approval_required"]
            contract_mismatch = bool(run and run.get("contract_id"))
            report = {
                "run_id": run_id,
                "contract_id": run.get("contract_id") if run else None,
                "contract_hash": run.get("contract_hash") if run else None,
                "status": (
                    "inconclusive"
                    if contract_mismatch or pending
                    else ("failed" if failed else "passed")
                ),
                "summary": {
                    "tool_calls": sum(event.get("event") == "tool_succeeded" for event in events),
                    "failures": len(failed),
                    "pending_approvals": len(pending),
                    **approval_digest(events, run_actions),
                },
                "acceptance_results": [],
            }
            report["verification_result"] = {
                "passed": "passed",
                "failed": "failed",
                "inconclusive": "inconclusive",
            }[report["status"]]

        events = self.store.get_run_events(run_id)
        run_actions = self.store.list_run_actions(run_id)
        report.setdefault("summary", {}).update(approval_digest(events, run_actions))

        reconciliation = None
        baseline = self.store.get_baseline(run_id)
        if baseline:
            after = capture_filesystem_snapshot(baseline["root_path"])
            diff = diff_filesystem_snapshots(baseline.get("snapshot", {}), after)
            allowed_patterns = contract.scope.can_write if contract else []
            reported_paths = [
                str(event.get("resource"))
                for event in self.store.get_run_events(run_id)
                if event.get("resource") and event.get("event") == "tool_succeeded" and event.get("capability") == "write"
            ]
            reconciliation = reconcile_changes(diff, allowed_patterns, reported_paths)
            self.store.set_run_reconciliation(run_id, reconciliation)
            report["reconciliation"] = reconciliation
            if reconciliation["status"] != "passed" and report["status"] == "passed":
                report["status"] = "inconclusive"
                report["verification_result"] = "inconclusive"
            self.store.create_checkpoint(
                run_id,
                "reconciliation",
                reconciliation["status"],
                "tool_generated",
                reconciliation,
            )
        work = self.store.get_record("work_item", process["work_item_id"], process["work_revision"])["payload"] if process else {}
        scenarios = evaluate_scenarios(work.get("scenarios", []), events, process["workspace_root"] if process else OFFICE_DIR)
        report["scenario_results"] = scenarios
        if scenarios:
            scenario_status = aggregate(scenarios)
            report["status"] = {"passed": "passed", "failed": "failed", "limited": "inconclusive", "not_run": "inconclusive"}[scenario_status]
            if any(not item["passed"] for item in report.get("acceptance_results", [])):
                report["status"] = "failed"
            if any(event.get("event") == "scope_violation" for event in events) or (reconciliation and reconciliation["status"] != "passed"):
                report["status"] = "inconclusive"
        if report["summary"].get("pending_approvals"):
            report["status"] = "inconclusive"
        if snapshot_changed or any(event.get("event") == "execution.outcome_unknown" for event in events):
            report["status"] = "inconclusive"
            report.setdefault("limitations", []).append("contract_changed_or_outcome_unknown")
        if stopped:
            report["status"] = "inconclusive"
            report.setdefault("limitations", []).append("run_stopped")
        report["verification_result"] = report["status"]
        self.verify_nodes(run_id, report)
        nodes = self.store.list_task_nodes(run_id)
        if nodes and any(node["status"] != "completed" for node in nodes) and report["status"] == "passed":
            report["status"] = report["verification_result"] = "inconclusive"
        report["task_plan"] = nodes
        report["summary"].update({"denied_attempts": sum(event.get("event") == "tool_denied" for event in events),
                                  "scope_violations": len((reconciliation or {}).get("scope_violations", [])),
                                  "historical_failures": sum(event.get("event") == "tool_failed" for event in events)})
        self.store.create_checkpoint(
            run_id,
            "verification",
            report["status"],
            "tool_generated",
            {"summary": report.get("summary", {}), "acceptance_results": report.get("acceptance_results", [])},
        )

        return report

    def verify_nodes(self, run_id, report):
        nodes = self.store.list_task_nodes(run_id)
        parents = {node.get("parent_node_id") for node in nodes}
        facts = {item["id"]: item["status"] for item in report.get("scenario_results", [])}
        facts.update({item["id"]: "passed" if item["passed"] else "failed" for item in report.get("acceptance_results", [])})
        for node in nodes:
            if node["node_id"] in parents or node["status"] not in {"ready_for_verification", "verifying", "running", "completed"}:
                continue
            references = [item.get("id") if isinstance(item, dict) else item for item in node.get("acceptance", [])]
            # A single execution node can use the full work item's AC set.
            if not references and len(nodes) == 1:
                references = list(facts)
            passed = bool(references) and all(facts.get(ref) == "passed" for ref in references)
            self.store.update_task_node(node["node_id"], status="completed" if passed else "waiting", output={"ac_ids": references, "verification_result": "passed" if passed else "limited"})
            self.store.append_run_event(run_id, "verification.completed", {"node_id": node["node_id"], "ac_ids": references, "status": "passed" if passed else "limited"})
        for node in reversed(self.store.list_task_nodes(run_id)):
            children = [child for child in self.store.list_task_nodes(run_id) if child.get("parent_node_id") == node["node_id"]]
            if children:
                self.store.update_task_node(node["node_id"], status="completed" if all(child["status"] == "completed" for child in children) else "pending")

    def revise_plan(self, run_id, steps, *, expected_revision, predicted_files=None):
        from .task_planner import validate_task_graph
        validate_task_graph(steps)
        process = self.store.get_process_run(run_id)
        plan = PlanRevision(work_item_id=process["work_item_id"], steps=steps, predicted_files=predicted_files or [])
        return self.store.revise_record("plan", run_id, plan.model_dump(), expected_revision=expected_revision, run_id=run_id)

    def revise_context(self, context, *, expected_revision, actor):
        context = ProductContext.model_validate(context)
        if actor in {"agent", "runtime", "model"} and any(entry.status == "confirmed" for entry in context.entries):
            raise ValueError("agent proposals cannot confirm product constraints")
        if actor in {"agent", "runtime", "model"}:
            active = self.store.get_record("product_context", context.project_id)
            if (active["revision"] if active else 0) != expected_revision:
                raise ValueError("revision_conflict")
            proposal = self.store.get_record("context_proposal", context.project_id)
            return self.store.revise_record("context_proposal", context.project_id, context.model_dump(), expected_revision=proposal["revision"] if proposal else 0, actor=actor)
        return self.store.revise_record("product_context", context.project_id, context.model_dump(), expected_revision=expected_revision, actor=actor)

    def next_after_verification(self, run_id, report):
        process = self.store.get_process_run(run_id)
        if report["status"] == "passed":
            return "finalize"
        if report.get("limitations"):
            return "finalize"
        nodes = self.store.list_task_nodes(run_id)
        if not any(node["status"] == "waiting" for node in nodes) and self.activate_next_task(run_id):
            self.store.transition_run(run_id, "executing")
            return "agent"
        failed = [item for item in report.get("scenario_results", []) if item["status"] in {"failed", "not_run"}]
        if failed and not report.get("limitations") and self.store.reserve_repair(run_id, {"failures": failed}):
            for node in self.store.list_task_nodes(run_id):
                if node["status"] == "waiting":
                    self.store.update_task_node(node["node_id"], status="running")
            self.store.transition_run(run_id, "executing")
            return "agent"
        request = DecisionRequest(kind="budget" if failed else "clarification",
            question="当前验证尚未通过，需要决定后续处理。",
            options=[{"id": "follow_up", "label": "在后继运行继续处理"}, {"id": "stop", "label": "结束本次尝试"}],
            blocked_node_ids=[node["node_id"] for node in nodes if node["status"] != "completed"])
        self.store.request_decision(run_id, "verification_follow_up", request.model_dump())
        return "finalize"

    def handle_control(self, run_id, text, actor="local_user"):
        normalized = text.strip().lower()
        if normalized in {"/status", "status", "状态", "查看状态", "进度"}:
            return {"kind": "status_query", "resume": self.prepare_resume(run_id)}
        if normalized in {"/cancel", "cancel", "取消"}:
            for action in self.store.list_run_actions(run_id):
                if action["status"] == "pending":
                    self.store.reject_pending_action(action["action_id"], actor)
            self.store.transition_run(run_id, "finished", result="cancelled")
            return {"kind": "cancel", "run_status": "finished", "terminal_result": "cancelled"}
        if normalized in {"/pause", "pause", "暂停"}:
            self.store.transition_run(run_id, "waiting", waiting={"reason": "user_pause", "blocked_node_ids": [], "resume_target": "executing"})
            return {"kind": "pause"}
        process = self.store.get_process_run(run_id)
        proposal_id = uuid.uuid4().hex
        self.store.revise_record("feedback", proposal_id, {"work_item_id": process["work_item_id"], "text": text, "status": "proposed"}, expected_revision=0, actor=actor, run_id=run_id)
        self.store.transition_run(run_id, "waiting", waiting={"reason": "feedback_requires_revision", "blocked_node_ids": [], "resume_target": "executing", "proposal_id": proposal_id})
        return {"kind": "feedback", "proposal_id": proposal_id, "status": "proposed"}

    def resolve_decision(self, request_id, revision, actor, choice):
        resolution = self.store.resolve_decision(request_id, revision, actor, choice)
        with self.store._connect() as conn:
            row = conn.execute("SELECT run_id, payload_json FROM decision_requests WHERE request_id=?", (request_id,)).fetchone()
        import json
        payload = json.loads(row["payload_json"])
        if payload["kind"] == "acceptance":
            process = self.store.get_process_run(row["run_id"])
            work = self.store.get_record("work_item", process["work_item_id"])
            changed = dict(work["payload"])
            changed["status"] = "archived" if choice == "accepted" else "needs_review"
            self.store.revise_record("work_item", process["work_item_id"], changed, expected_revision=work["revision"], actor=actor, run_id=row["run_id"])
            # Preserve the delivery issued by this run; human resolution is a separate revision.
            self.store.revise_record("human_acceptance", row["run_id"], resolution, expected_revision=0, actor=actor, run_id=row["run_id"])
        return resolution

    def build_delivery(self, run_id, report):
        process = self.store.get_process_run(run_id)
        work = self.store.get_record("work_item", process["work_item_id"], process["work_revision"])["payload"]
        technical = {"passed": "passed", "failed": "failed", "inconclusive": "limited"}[report["status"]]
        report["delivery"] = Delivery(technical_status=technical,
            scenario_status=aggregate(report.get("scenario_results", [])),
            human_acceptance="pending" if work["acceptance_required"] and technical == "passed" else "not_requested",
            acceptance_required=work["acceptance_required"], limitations=report.get("limitations", []),
            artifact_refs=(process.get("contract") or {}).get("deliverables", [])).model_dump()
        report.update({"work_item_id": process["work_item_id"], "run_id": run_id,
                       "plan_revision": (self.store.get_record("plan", run_id) or {}).get("revision"),
                       "context_revision": process["context_revision"],
                       "task_plan": self.store.list_task_nodes(run_id),
                       "checkpoints": self.store.list_checkpoints(run_id),
                       "effective_policy": self.store.get_effective_policy(run_id) or {},
                       "event_watermark": len(self.store.get_run_events(run_id)),
                       "acceptance_decision": None, "closure_result": "succeeded" if technical == "passed" else "needs_review"})
        return report

    def close_attempt(self, run_id, report):
        result = "succeeded" if report["status"] == "passed" else "needs_review"
        existing = self.store.get_process_run(run_id)
        if existing["run_status"] == "finished":
            result = existing["terminal_result"]
        self.store.transition_run(run_id, "finished", result=result)
        process = self.store.get_process_run(run_id)
        work = self.store.get_record("work_item", process["work_item_id"])
        payload = dict(work["payload"])
        payload["status"] = "awaiting_acceptance" if report["delivery"]["human_acceptance"] == "pending" else ("archived" if result == "succeeded" else "needs_review")
        self.store.revise_record("work_item", process["work_item_id"], payload, expected_revision=work["revision"], run_id=run_id)
        report.update({"run_status": "finished", "terminal_result": result})
        if report["delivery"]["human_acceptance"] == "pending":
            decision = DecisionRequest(kind="acceptance", question="接受本次交付成果？", options=[{"id": "accepted", "label": "接受"}, {"id": "rejected", "label": "返工"}])
            self.store.request_decision(run_id, "delivery_acceptance", decision.model_dump())
        self.store.save_delivery(run_id, report)
        write_report(report.get("contract_id") or f"run-{run_id}", report, run_id=run_id)
        return report

    def finalize(self, run_id, thread_id, report=None):
        process = self.store.get_process_run(run_id)
        if process and process["run_status"] == "finished" and process.get("delivery"):
            return process["delivery"]
        report = report or self.evaluate_run(run_id, thread_id)
        return self.close_attempt(run_id, self.build_delivery(run_id, report))

    def prepare_resume(self, run_id):
        process = self.store.get_process_run(run_id)
        if not process:
            raise ValueError("unknown process run")
        work = self.store.get_record("work_item", process["work_item_id"], process["work_revision"])
        context = self.store.get_record("product_context", work["payload"]["project_id"], process["context_revision"]) if process["context_revision"] else None
        acceptance = self.store.get_record("human_acceptance", run_id)
        return {"work_item": work["payload"], "run_status": process["run_status"],
                "waiting": process["waiting"], "remaining_repairs": process["max_repairs"] - process["repair_count"],
                "nodes": self.store.list_task_nodes(run_id), "decisions": self.store.list_decisions(run_id),
                "product_context": context["payload"] if context else None,
                "human_acceptance": acceptance["payload"] if acceptance else (process.get("delivery") or {}).get("delivery", {}).get("human_acceptance", "not_requested"),
                "contract_hash": self.store.get_run(run_id)["contract_hash"],
                "workspace": capture_filesystem_snapshot(process["workspace_root"])}


process_manager = ProcessManager()
