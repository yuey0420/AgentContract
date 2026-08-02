import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from ..config import ACTIVE_CONTRACT_FILE, CONTRACT_REPORTS_DIR
from .models import ApprovalInfo, TaskContract


def _canonical_contract_payload(contract: TaskContract) -> dict[str, Any]:
    payload = contract.model_dump(mode="json")
    approval = payload.get("approval")
    if isinstance(approval, dict):
        approval["contract_hash"] = None
    return payload


def compute_contract_hash(contract: TaskContract) -> str:
    payload = _canonical_contract_payload(contract)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def contract_has_valid_approval(contract: TaskContract) -> bool:
    if contract.approval is None or not contract.approval.contract_hash:
        return False
    return contract.approval.contract_hash == compute_contract_hash(contract)


def approve_contract(contract: TaskContract, approved_by: str, mode: str = "explicit") -> TaskContract:
    approved = contract.model_copy(deep=True)
    approved.status = "approved"
    approved.approval = ApprovalInfo(
        mode=mode,
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    approved.approval.contract_hash = compute_contract_hash(approved)
    return approved


def write_active_contract(contract: TaskContract) -> str:
    os.makedirs(os.path.dirname(ACTIVE_CONTRACT_FILE), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix="current.contract.",
        suffix=".tmp",
        dir=os.path.dirname(ACTIVE_CONTRACT_FILE),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(contract.model_dump(mode="json"), file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, ACTIVE_CONTRACT_FILE)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    return ACTIVE_CONTRACT_FILE


def approve_active_contract(approved_by: str) -> TaskContract:
    from ..runtime_store import runtime_store

    contract = load_active_contract()
    if contract is None:
        raise FileNotFoundError("没有 active contract 可批准")
    approved = approve_contract(contract, approved_by, mode="registry")
    write_active_contract(approved)
    runtime_store.record_contract_approval(
        approved.id,
        approved.approval.contract_hash,
        approved_by,
        approved.approval.approved_at,
    )
    return approved


def load_active_contract() -> TaskContract | None:
    if not os.path.exists(ACTIVE_CONTRACT_FILE):
        return None

    with open(ACTIVE_CONTRACT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return TaskContract.model_validate(data)


def write_report(contract_id: str, report: dict[str, Any], run_id: str | None = None) -> str:
    os.makedirs(CONTRACT_REPORTS_DIR, exist_ok=True)
    safe_id = "".join(c for c in contract_id if c.isalnum() or c in "-_") or "contract"
    safe_run = "".join(c for c in (run_id or "") if c.isalnum() or c in "-_")
    filename = f"{safe_id}.{safe_run}.report.json" if safe_run else f"{safe_id}.report.json"
    path = os.path.join(CONTRACT_REPORTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path
