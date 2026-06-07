import hashlib
import json
import os
from typing import Any

from ..config import ACTIVE_CONTRACT_FILE, CONTRACT_REPORTS_DIR
from .models import TaskContract


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


def load_active_contract() -> TaskContract | None:
    if not os.path.exists(ACTIVE_CONTRACT_FILE):
        return None

    with open(ACTIVE_CONTRACT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return TaskContract.model_validate(data)


def write_report(contract_id: str, report: dict[str, Any]) -> str:
    os.makedirs(CONTRACT_REPORTS_DIR, exist_ok=True)
    safe_id = "".join(c for c in contract_id if c.isalnum() or c in "-_") or "contract"
    path = os.path.join(CONTRACT_REPORTS_DIR, f"{safe_id}.report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path
