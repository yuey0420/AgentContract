"""Deterministic scenario evidence evaluation; model text is never evidence."""

from .evidence import capture_filesystem_snapshot, snapshot_fingerprint


def evaluate_scenarios(scenarios, events, root):
    snapshot = capture_filesystem_snapshot(root)
    fingerprint = snapshot_fingerprint(snapshot)
    results = []
    for scenario in scenarios:
        latest = next((event for event in reversed(events)
                       if event.get("tool") == "run_office_check"
                       and (event.get("result") or {}).get("check_id") == scenario["check_id"]), None)
        evidence = (latest or {}).get("result") or {}
        status = "not_run"
        limitations = []
        if latest:
            if evidence.get("status") != "succeeded" or evidence.get("exit_code") != 0:
                status = "failed"
            elif evidence.get("workspace_revision") != fingerprint:
                status = "limited"
                limitations.append("artifact_revision_changed")
            elif snapshot["coverage"] != "complete" or evidence.get("coverage") != "complete":
                status = "limited"
                limitations.append("incomplete_snapshot")
            else:
                status = "passed"
        results.append({"id": scenario["id"], "check_id": scenario["check_id"],
                        "kind": scenario.get("kind", "scenario"), "status": status,
                        "required": scenario.get("required", True),
                        "workspace_revision": fingerprint, "checker_version": evidence.get("checker_version"),
                        "evidence_refs": [latest["tool_call_id"]] if latest else [],
                        "limitations": limitations, "exit_code": evidence.get("exit_code")})
    return results


def aggregate(results):
    required = [item for item in results if item.get("required", True)]
    if not required:
        return "not_run"
    if any(item["status"] == "failed" for item in required):
        return "failed"
    return "passed" if all(item["status"] == "passed" for item in required) else "limited"
