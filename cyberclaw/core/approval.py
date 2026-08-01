from dataclasses import dataclass
from typing import Any

from .logger import audit_logger
from .runtime_store import RuntimeStore, runtime_store


@dataclass(frozen=True)
class ApprovalResult:
    action_id: str
    status: str
    action: dict[str, Any] | None


class ApprovalService:
    """Owns the approval state machine and its audit trail."""

    def __init__(self, store: RuntimeStore = runtime_store):
        self.store = store

    def request(
        self,
        *,
        run_id: str,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        contract_hash: str | None,
        risk_level: str | None,
        clause: str | None,
        reason: str,
        ttl_minutes: int = 15,
    ) -> dict[str, Any]:
        action_id = self.store.create_pending_action(
            run_id,
            thread_id,
            tool_name,
            args,
            contract_hash,
            ttl_minutes,
            tool_call_id=tool_call_id,
            risk_level=risk_level or "high",
            clause=clause,
            reason=reason,
        )
        action = self.store.get_action(action_id)
        if action and action["status"] == "pending":
            events = self.store.get_run_events(run_id)
            if not any(
                event.get("event") == "approval_required" and event.get("action_id") == action_id
                for event in events
            ):
                self.store.append_run_event(run_id, "approval_required", {
                    "action_id": action_id,
                    "tool": tool_name,
                    "tool_call_id": tool_call_id,
                    "risk_level": risk_level or "high",
                    "clause": clause,
                })
        return action or {"action_id": action_id, "status": "missing"}

    def approve(self, action_id: str, actor: str = "local_user") -> ApprovalResult:
        changed = self.store.approve_pending_action(action_id, actor)
        action = self.store.get_action(action_id)
        self._audit(action, "approval_approved" if changed else "approval_approve_failed", actor)
        return ApprovalResult(action_id, action["status"] if action else "missing", action)

    def reject(self, action_id: str, actor: str = "local_user") -> ApprovalResult:
        changed = self.store.reject_pending_action(action_id, actor)
        action = self.store.get_action(action_id)
        self._audit(action, "approval_rejected" if changed else "approval_reject_failed", actor)
        return ApprovalResult(action_id, action["status"] if action else "missing", action)

    def consume_exact(
        self,
        action_id: str,
        *,
        thread_id: str,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        contract_hash: str | None,
    ) -> ApprovalResult:
        consumed = self.store.consume_action(
            action_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=args,
            contract_hash=contract_hash,
        )
        action = self.store.get_action(action_id)
        self._audit(action, "approval_consumed" if consumed else "approval_consume_failed", None)
        return ApprovalResult(action_id, action["status"] if action else "missing", action)

    def get(self, action_id: str) -> ApprovalResult:
        action = self.store.get_action(action_id)
        return ApprovalResult(action_id, action["status"] if action else "missing", action)

    @staticmethod
    def _audit(action: dict[str, Any] | None, event: str, actor: str | None):
        audit_logger.log_event(
            thread_id=(action or {}).get("thread_id", "system_default"),
            event=event,
            run_id=(action or {}).get("run_id"),
            action_id=(action or {}).get("action_id"),
            tool=(action or {}).get("tool_name"),
            actor=actor,
            status=(action or {}).get("status", "missing"),
        )


approval_service = ApprovalService()
