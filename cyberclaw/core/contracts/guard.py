from typing import Any

from ..logger import audit_logger
from .models import ContractDecision
from .policy import allow, check_shell_command, check_tool_allowed, check_write_path, require_confirmation
from .store import compute_contract_hash, load_active_contract


def _log_contract_event(thread_id: str, event: str, decision: ContractDecision, tool_name: str, args: dict[str, Any]):
    audit_logger.log_event(
        thread_id=thread_id,
        event=event,
        contract_id=decision.contract_id,
        contract_hash=decision.contract_hash,
        decision=decision.decision,
        risk_level=decision.risk_level,
        clause=decision.clause,
        reason=decision.reason,
        tool=tool_name,
        args=args,
    )


def guard_tool_call(thread_id: str, tool_name: str, args: dict[str, Any]) -> ContractDecision:
    try:
        contract = load_active_contract()
    except Exception as e:
        decision = ContractDecision(
            decision="deny",
            clause="contract.load",
            reason=f"active contract 加载失败：{e}",
        )
        _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        return decision

    if contract is None:
        decision = ContractDecision(decision="allow", reason="未配置 active contract，保持兼容模式")
        _log_contract_event(thread_id, "contract_check", decision, tool_name, args)
        return decision

    contract_hash = compute_contract_hash(contract)
    audit_logger.log_event(
        thread_id=thread_id,
        event="contract_loaded",
        contract_id=contract.id,
        contract_hash=contract_hash,
        risk_level=contract.risk_level,
        status=contract.status,
    )

    if contract.status != "approved":
        decision = ContractDecision(
            decision="deny",
            contract_id=contract.id,
            contract_hash=contract_hash,
            clause="contract.status",
            reason=f"active contract 状态为 {contract.status}，不是 approved",
            risk_level=contract.risk_level,
        )
        _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        return decision

    decision = check_tool_allowed(contract, tool_name)
    if decision.decision != "allow":
        _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        return decision

    if tool_name == "write_office_file":
        decision = check_write_path(contract, str(args.get("filepath", "")))
    elif tool_name == "execute_office_shell":
        decision = check_shell_command(contract, str(args.get("command", "")))
    else:
        decision = allow(contract, "tool_policy.allowed_tools", f"工具 {tool_name} 通过通用契约校验")

    if decision.decision != "allow":
        _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        return decision

    if tool_name in contract.tool_policy.require_confirmation_for:
        decision = require_confirmation(
            contract,
            "tool_policy.require_confirmation_for",
            f"工具 {tool_name} 属于需要人工确认的高风险工具",
        )
        _log_contract_event(thread_id, "contract_confirmation_required", decision, tool_name, args)
        return decision

    _log_contract_event(thread_id, "contract_check", decision, tool_name, args)
    return decision


def format_contract_denial(decision: ContractDecision) -> str:
    if decision.decision == "require_confirmation":
        return (
            "契约要求人工确认：本次工具调用属于高风险动作。\n"
            f"契约：{decision.contract_id or '无'}\n"
            f"条款：{decision.clause or '未指定'}\n"
            f"原因：{decision.reason}\n"
            "请用户明确确认后，再继续执行。"
        )

    return (
        "契约拒绝：本次工具调用违反 active contract。\n"
        f"契约：{decision.contract_id or '无'}\n"
        f"条款：{decision.clause or '未指定'}\n"
        f"原因：{decision.reason}"
    )
