import json
import os
from datetime import datetime, timezone
from typing import Any

from ..config import OFFICE_DIR, PROJECT_ROOT
from ..logger import audit_logger
from .models import TaskContract
from .store import write_report


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
    target = os.path.abspath(os.path.join(OFFICE_DIR, path))
    base = os.path.abspath(OFFICE_DIR)
    if os.path.commonpath([base, target]) != base:
        return False
    return os.path.exists(target)


def generate_contract_report(contract: TaskContract, thread_id: str = "local_geek_master") -> dict[str, Any]:
    events = [e for e in _read_log_events(thread_id) if e.get("contract_id") == contract.id]
    violations = [e for e in events if e.get("event") == "contract_violation"]
    denied = [e for e in events if e.get("decision") == "deny"]
    confirmations = [e for e in events if e.get("event") == "contract_confirmation_required"]
    tool_calls = [e for e in events if e.get("event") in {"contract_check", "contract_confirmation_required"}]

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
            passed = any(e.get("tool") == rule.tool for e in events)
            evidence = [e for e in events if e.get("tool") == rule.tool]
        elif rule.type == "tool_not_called":
            matching = [e for e in events if e.get("tool") == rule.tool]
            passed = not matching
            evidence = matching
        else:
            evidence = [{"unsupported_rule": rule.type}]

        acceptance_results.append({
            "type": rule.type,
            "passed": passed,
            "evidence": evidence,
        })

    overall_passed = all(item["passed"] for item in acceptance_results) if acceptance_results else not violations
    report = {
        "contract_id": contract.id,
        "status": "passed" if overall_passed else "failed",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "tool_calls": len(tool_calls),
            "violations": len(violations),
            "denied": len(denied),
            "requires_confirmation": len(confirmations),
        },
        "acceptance_results": acceptance_results,
    }
    write_report(contract.id, report)
    audit_logger.log_event(
        thread_id=thread_id,
        event="contract_acceptance",
        contract_id=contract.id,
        status=report["status"],
        summary=report["summary"],
    )
    return report
