from typing import Any

from ..contracts.report import approval_digest, generate_contract_report
from ..contracts.store import compute_contract_hash, load_active_contract, write_report
from ..config import OFFICE_DIR
from ..logger import audit_logger
from ..runtime_store import RuntimeStore, runtime_store
from .capabilities import assess_contract_capabilities
from .evidence import capture_filesystem_snapshot, diff_filesystem_snapshots, reconcile_changes
from .task_planner import decompose_objective


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
            audited = process_profile == "audited" or bool(contract.inputs.get("capture_baseline"))
            if audited:
                snapshot = capture_filesystem_snapshot(OFFICE_DIR)
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
        leaves = [node for node in nodes if node.get("parent_node_id")]
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
        return None

    def complete_active_task(self, run_id: str, output: dict[str, Any] | None = None) -> dict[str, Any] | None:
        nodes = self.store.list_task_nodes(run_id)
        current = next(
            (node for node in nodes if node.get("parent_node_id") and node.get("status") == "running"),
            None,
        )
        if not current:
            return self.activate_next_task(run_id)
        self.store.update_task_node(current["node_id"], status="completed", output=output or {})
        self.store.append_run_event(run_id, "task_completed", {
            "node_id": current["node_id"],
            "output": output or {},
        })
        return self.activate_next_task(run_id)

    def finalize(self, run_id: str, thread_id: str) -> dict[str, Any]:
        self.store.update_run(run_id, phase="verify", status="running")
        run = self.store.get_run(run_id)
        contract = None
        try:
            candidate = load_active_contract()
            if candidate and run and run.get("contract_hash") == compute_contract_hash(candidate):
                contract = candidate
        except Exception:
            contract = None

        audit_logger.flush()
        if contract:
            report = generate_contract_report(contract, thread_id=thread_id, run_id=run_id, store=self.store)
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
            write_report(f"run-{run_id}", report, run_id=run_id)

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
                if event.get("resource")
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
            write_report(
                contract.id if contract else f"run-{run_id}",
                report,
                run_id=run_id,
            )

        task_nodes = self.store.list_task_nodes(run_id)
        node_status = "completed" if report["status"] == "passed" else (
            "failed" if report["status"] == "failed" else "needs_review"
        )
        for node in task_nodes:
            if node["node_id"] != node.get("parent_node_id"):
                self.store.update_task_node(
                    node["node_id"], status=node_status,
                    output={"verification_result": report.get("verification_result", report["status"])},
                )
        self.store.create_checkpoint(
            run_id,
            "verification",
            report["status"],
            "tool_generated",
            {"summary": report.get("summary", {}), "acceptance_results": report.get("acceptance_results", [])},
        )

        acceptance_decision = {
            "passed": "accepted",
            "failed": "rejected",
            "inconclusive": "needs_review",
        }.get(report["status"], "needs_review")
        self.store.decide_acceptance(
            run_id,
            acceptance_decision,
            decided_by="runtime",
            reason=report.get("status", "unknown"),
        )
        closure_result = "accepted" if acceptance_decision == "accepted" else "needs_review"
        self.store.close_run(run_id, closed_by="runtime", result=closure_result)
        status = "completed" if report["status"] == "passed" else "needs_review"
        self.store.append_run_event(run_id, "run_verified", {
            "status": report["status"],
            "summary": report.get("summary", {}),
        })
        # Keep the legacy terminal status update for callers using a custom store.
        run_after_close = self.store.get_run(run_id)
        if not run_after_close or run_after_close.get("status") != status:
            self.store.update_run(run_id, phase="finalize", status=status)
        if run_after_close:
            report.update({
                "task_id": run_after_close.get("task_id"),
                "task_version": run_after_close.get("task_version"),
                "plan_version": run_after_close.get("plan_version"),
                "policy_version": run_after_close.get("policy_version"),
                "acceptance_decision": run_after_close.get("acceptance_decision"),
                "closure_result": run_after_close.get("closure_result"),
                "task_plan": self.store.list_task_nodes(run_id),
                "effective_policy": self.store.get_effective_policy(run_id) or {},
                "reconciliation": self.store.get_run_reconciliation(run_id) or reconciliation,
                "checkpoints": self.store.list_checkpoints(run_id),
                "repositories": self.store.list_repositories(run_id),
            })
        audit_logger.log_event(
            thread_id=thread_id,
            event="process_completed",
            run_id=run_id,
            status=report["status"],
        )
        return report


process_manager = ProcessManager()
