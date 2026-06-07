import fnmatch

from .models import ContractDecision, TaskContract
from .store import compute_contract_hash


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _matches_any(value: str, patterns: list[str]) -> tuple[bool, str | None]:
    normalized = _normalize_path(value)
    for pattern in patterns:
        normalized_pattern = _normalize_path(pattern)
        if fnmatch.fnmatch(normalized, normalized_pattern):
            return True, pattern
    return False, None


def _decision(
    contract: TaskContract,
    decision: str,
    clause: str,
    reason: str,
) -> ContractDecision:
    return ContractDecision(
        decision=decision,  # type: ignore[arg-type]
        contract_id=contract.id,
        clause=clause,
        reason=reason,
        risk_level=contract.risk_level,
        contract_hash=compute_contract_hash(contract),
    )


def allow(contract: TaskContract, clause: str = "contract.allow", reason: str = "契约允许执行") -> ContractDecision:
    return _decision(contract, "allow", clause, reason)


def deny(contract: TaskContract, clause: str, reason: str) -> ContractDecision:
    return _decision(contract, "deny", clause, reason)


def require_confirmation(contract: TaskContract, clause: str, reason: str) -> ContractDecision:
    return _decision(contract, "require_confirmation", clause, reason)


def check_tool_allowed(contract: TaskContract, tool_name: str) -> ContractDecision:
    policy = contract.tool_policy

    if tool_name in policy.blocked_tools:
        return deny(contract, "tool_policy.blocked_tools", f"工具 {tool_name} 命中 blocked_tools")

    if not policy.allowed_tools:
        return deny(contract, "tool_policy.allowed_tools", "active contract 未声明 allowed_tools，默认不允许工具执行")

    if tool_name not in policy.allowed_tools:
        return deny(contract, "tool_policy.allowed_tools", f"工具 {tool_name} 不在 allowed_tools 中")

    return allow(contract, "tool_policy.allowed_tools", f"工具 {tool_name} 在 allowed_tools 中")


def check_write_path(contract: TaskContract, filepath: str) -> ContractDecision:
    blocked, blocked_pattern = _matches_any(filepath, contract.scope.cannot_write)
    if blocked:
        return deny(
            contract,
            "scope.cannot_write",
            f"路径 {_normalize_path(filepath)} 命中 cannot_write: {blocked_pattern}",
        )

    if not contract.scope.can_write:
        return deny(contract, "scope.can_write", "active contract 未声明 can_write，默认不允许写文件")

    allowed, allowed_pattern = _matches_any(filepath, contract.scope.can_write)
    if not allowed:
        return deny(contract, "scope.can_write", f"路径 {_normalize_path(filepath)} 未命中 can_write")

    return allow(contract, "scope.can_write", f"路径 {_normalize_path(filepath)} 命中 can_write: {allowed_pattern}")


def _blocked_command(command: str, rule: str) -> bool:
    cmd = command.strip().lower()
    normalized_rule = rule.strip().lower()
    return (
        fnmatch.fnmatch(cmd, normalized_rule)
        or cmd == normalized_rule
        or cmd.startswith(normalized_rule + " ")
        or normalized_rule in cmd
    )


def _allowed_command(command: str, rule: str) -> bool:
    cmd = command.strip().lower()
    normalized_rule = rule.strip().lower()
    return (
        fnmatch.fnmatch(cmd, normalized_rule)
        or cmd == normalized_rule
        or cmd.startswith(normalized_rule + " ")
    )


def check_shell_command(contract: TaskContract, command: str) -> ContractDecision:
    for rule in contract.scope.cannot_execute:
        if _blocked_command(command, rule):
            return deny(contract, "scope.cannot_execute", f"命令 `{command}` 命中 cannot_execute: {rule}")

    if not contract.scope.can_execute:
        return deny(contract, "scope.can_execute", "active contract 未声明 can_execute，默认不允许执行 shell")

    for rule in contract.scope.can_execute:
        if _allowed_command(command, rule):
            return allow(contract, "scope.can_execute", f"命令 `{command}` 命中 can_execute: {rule}")

    return deny(contract, "scope.can_execute", f"命令 `{command}` 未命中 can_execute")
