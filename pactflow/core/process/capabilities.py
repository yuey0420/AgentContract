"""Deterministic capability and effective-policy assessment."""

from __future__ import annotations

from typing import Any

from ..contracts.models import TaskContract


def assess_contract_capabilities(contract: TaskContract) -> dict[str, Any]:
    scope = contract.scope
    policy = contract.tool_policy
    requested = {
        "read": list(scope.can_read),
        "write": list(scope.can_write),
        "execute": list(scope.can_execute),
        "tools": list(policy.allowed_tools),
    }
    effective = {
        "read": [path for path in scope.can_read if path not in scope.forbidden],
        "write": [
            path for path in scope.can_write
            if path not in scope.cannot_write and path not in scope.forbidden
        ],
        "execute": [
            command for command in scope.can_execute
            if command not in scope.cannot_execute and command not in scope.forbidden
        ],
        "tools": [tool for tool in policy.allowed_tools if tool not in policy.blocked_tools],
    }
    enforcement = {
        "tool_gateway": True,
        "scope_guard": bool(
            scope.can_read or scope.can_write or scope.can_execute
            or scope.cannot_write or scope.cannot_execute or scope.forbidden
        ),
        "approval_gate": bool(
            policy.high_risk_tools or policy.require_confirmation_for
            or contract.risk_level in {"high", "critical"}
        ),
        "limits": contract.limits.model_dump(mode="json"),
    }
    return {
        "requested": requested,
        "effective": effective,
        "enforcement": enforcement,
        "policy_version": contract.policy_version,
    }

