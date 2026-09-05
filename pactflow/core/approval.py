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
        capability: str | None = None,
        resource: str | None = None,
        process_profile: str | None = None,
        effect: str | None = None,
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
            capability=capability,
            resource=resource,
            process_profile=process_profile,
            effect=effect,
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

    def approve_scope(
        self,
        action_id: str,
        actor: str = "local_user",
    ) -> ApprovalResult:
        result = self.approve(action_id, actor)
        action = result.action
        if result.status != "approved" or not action:
            return result
        if not self.scope_eligible(action):
            return result
        profile = str(action.get("process_profile") or "development")
        self.store.create_approval_grant(
            run_id=str(action["run_id"]),
            thread_id=str(action["thread_id"]),
            contract_hash=str(action["contract_hash"]),
            tool_name=str(action["tool_name"]),
            capability=str(action["capability"]),
            resource_pattern=self._resource_pattern(
                str(action.get("resource") or ""), profile
            ),
            approved_by=actor,
            ttl_minutes=15,
            max_uses=5 if profile == "audited" else 20,
            source_action_id=action_id,
        )
        return result

    def consume_matching_grant(
        self,
        *,
        run_id: str,
        thread_id: str,
        contract_hash: str | None,
        tool_name: str,
        capability: str,
        resource: str | None,
    ) -> dict[str, Any] | None:
        if not contract_hash:
            return None
        return self.store.consume_matching_approval_grant(
            run_id=run_id,
            thread_id=thread_id,
            contract_hash=contract_hash,
            tool_name=tool_name,
            capability=capability,
            resource=resource,
        )

    @staticmethod
    def scope_eligible(action: dict[str, Any]) -> bool:
        risk = str(action.get("risk_level") or "high")
        clause = str(action.get("clause") or "")
        profile = str(action.get("process_profile") or "chat")
        capability = str(action.get("capability") or "")
        effect = str(action.get("effect") or capability)
        return bool(
            action.get("contract_hash")
            and profile in {"development", "audited"}
            and risk in {"low", "medium"}
            and capability in {"read", "write"}
            and str(action.get("tool_name", "")) in {"read_office_file", "list_office_files", "write_office_file", "patch_office_file"}
            and effect not in {"destructive", "external"}
            and clause in {"scope.review_required", "tool_policy.require_confirmation_for"}
        )

    @staticmethod
    def _resource_pattern(resource: str, profile: str) -> str:
        # Literal resource only; no implicit directory or interpreter expansion.
        return resource.replace("\\", "/")

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
        status = action["status"] if action else "missing"
        if not consumed and status == "consumed":
            status = "already_consumed"
        return ApprovalResult(action_id, status, action)

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
