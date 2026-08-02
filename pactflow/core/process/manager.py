from typing import Any

from ..contracts.report import generate_contract_report
from ..contracts.store import compute_contract_hash, load_active_contract, write_report
from ..logger import audit_logger
from ..runtime_store import RuntimeStore, runtime_store


class ProcessManager:
    def __init__(self, store: RuntimeStore = runtime_store):
        self.store = store

    def start(self, thread_id: str, objective: str) -> dict[str, Any]:
        contract = None
        try:
            contract = load_active_contract()
        except Exception:
            contract = None

        contract_hash = compute_contract_hash(contract) if contract else None
        mode = "managed_task" if contract else "chat"
        run_id = self.store.start_run(
            thread_id,
            objective,
            mode,
            contract.id if contract else None,
            contract_hash,
        )
        self.store.append_run_event(run_id, "run_started", {
            "mode": mode,
            "contract_id": contract.id if contract else None,
            "contract_hash": contract_hash,
        })
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
            "process_report": {},
        }

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
                },
                "acceptance_results": [],
            }
            write_report(f"run-{run_id}", report, run_id=run_id)

        status = "completed" if report["status"] == "passed" else "needs_review"
        self.store.append_run_event(run_id, "run_verified", {
            "status": report["status"],
            "summary": report.get("summary", {}),
        })
        self.store.update_run(run_id, phase="finalize", status=status)
        audit_logger.log_event(
            thread_id=thread_id,
            event="process_completed",
            run_id=run_id,
            status=report["status"],
        )
        return report


process_manager = ProcessManager()
