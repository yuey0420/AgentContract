import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from ..config import OFFICE_DIR, PROJECT_ROOT
from ..logger import audit_logger
from .models import TaskContract
from .store import write_report
from .store import compute_contract_hash
from ..runtime_store import runtime_store


def _read_log_events(thread_id: str) -> list[dict[str, Any]]:
    safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"
    log_path = os.path.join(PROJECT_ROOT, "logs", f"{safe_id}.jsonl")
    if not os.path.exists(log_path):
        return []

    events = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _file_exists(path: str | None) -> bool:
    if not path:
        return False
    base = Path(OFFICE_DIR).resolve(strict=False)
    target = (base / path).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError:
        return False
    return target.exists()


def generate_contract_report(
    contract: TaskContract,
    thread_id: str = "local_geek_master",
    run_id: str | None = None,
    store=runtime_store,
) -> dict[str, Any]:
    if run_id:
        events = store.get_run_events(run_id)
        violations = [e for e in events if e.get("event") == "tool_denied"]
        denied = violations
        run_actions = store.list_run_actions(run_id)
        confirmations = [
            action for action in run_actions
            if action.get("status") in {"pending", "approved"}
        ]
        if not run_actions:
            confirmations = [event for event in events if event.get("event") == "approval_required"]
        tool_calls = [e for e in events if e.get("event") == "tool_succeeded"]
    else:
        events = [e for e in _read_log_events(thread_id) if e.get("contract_id") == contract.id]
        violations = [e for e in events if e.get("event") == "contract_violation"]
        denied = [e for e in events if e.get("decision") == "deny"]
        confirmations = [e for e in events if e.get("event") == "contract_confirmation_required"]
        tool_calls = [e for e in events if e.get("event") == "contract_check"]

    acceptance_results = []
    for rule in contract.acceptance:
        passed = False
        evidence: list[Any] = []

        if rule.type == "no_contract_violation":
            passed = not violations
            evidence = violations
        elif rule.type == "file_exists":
            passed = _file_exists(rule.path)
            evidence = [{"path": rule.path, "exists": passed}]
        elif rule.type == "tool_called":
            passed = any(e.get("tool") == rule.tool for e in tool_calls)
            evidence = [e for e in tool_calls if e.get("tool") == rule.tool]
        elif rule.type == "tool_not_called":
            matching = [e for e in tool_calls if e.get("tool") == rule.tool]
            passed = not matching
            evidence = matching
        else:
            evidence = [{"unsupported_rule": rule.type}]

        acceptance_results.append({
            "type": rule.type,
            "passed": passed,
            "evidence": evidence,
        })

    overall_passed = all(item["passed"] for item in acceptance_results) if acceptance_results else None
    if confirmations:
        report_status = "inconclusive"
    elif overall_passed is None:
        report_status = "inconclusive"
    else:
        report_status = "passed" if overall_passed else "failed"
    report = {
        "contract_id": contract.id,
        "contract_hash": compute_contract_hash(contract),
        "run_id": run_id,
        "status": report_status,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "tool_calls": len(tool_calls),
            "violations": len(violations),
            "denied": len(denied),
            "requires_confirmation": len(confirmations),
        },
        "acceptance_results": acceptance_results,
    }
    write_report(contract.id, report, run_id=run_id)
    audit_logger.log_event(
        thread_id=thread_id,
        event="contract_acceptance",
        contract_id=contract.id,
        status=report["status"],
        summary=report["summary"],
    )
    return report
