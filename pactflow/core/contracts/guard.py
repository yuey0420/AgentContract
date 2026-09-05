from typing import Any

from ..logger import audit_logger
from .models import ContractDecision, TaskContract
from .policy import (
    allow,
    check_read_path,
    check_resource_boundary,
    check_shell_command,
    check_tool_allowed,
    check_write_path,
    require_confirmation,
)
from .store import compute_contract_hash, contract_has_valid_approval, load_active_contract
from ..runtime_store import runtime_store
from ..config import STRICT_CONTRACTS


_BASELINE_CONFIRMATION_TOOLS = {
    "delete_scheduled_task",
    "modify_scheduled_task",
}


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


def guard_tool_call(
    thread_id: str,
    tool_name: str,
    args: dict[str, Any],
    *,
    capability: str | None = None,
    resource: str | None = None,
    effect: str | None = None,
    contract_snapshot: TaskContract | None = None,
) -> ContractDecision:
    try:
        contract = contract_snapshot or load_active_contract()
    except Exception as e:
        decision = ContractDecision(
            decision="deny",
            clause="contract.load",
            reason=f"active contract 加载失败：{e}",
        )
        _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        return decision

    if contract is None:
        needs_confirmation = capability in {"execute", "external"} or tool_name in _BASELINE_CONFIRMATION_TOOLS
        if STRICT_CONTRACTS and capability in {"write", "execute", "external"}:
            decision = ContractDecision(
                decision="deny",
                clause="runtime.strict_contract_required",
                reason="严格模式要求副作用工具必须绑定 active contract",
            )
            _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        elif needs_confirmation:
            decision = ContractDecision(
                decision="require_confirmation",
                clause="runtime.baseline_confirmation",
                reason=f"无 active contract 时，{tool_name} 必须经过一次性人工批准",
            )
            _log_contract_event(thread_id, "contract_confirmation_required", decision, tool_name, args)
        else:
            decision = ContractDecision(decision="allow", reason="未配置 active contract，按基础策略执行")
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

    if not contract_has_valid_approval(contract):
        decision = ContractDecision(
            decision="deny",
            contract_id=contract.id,
            contract_hash=contract_hash,
            clause="contract.approval.contract_hash",
            reason="active contract 的批准哈希缺失或与当前内容不一致",
            risk_level=contract.risk_level,
        )
        _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        return decision

    if (
        contract.approval
        and contract.approval.mode == "registry"
        and not runtime_store.has_contract_approval(contract.id, contract_hash)
    ):
        decision = ContractDecision(
            decision="deny",
            contract_id=contract.id,
            contract_hash=contract_hash,
            clause="contract.approval.registry",
            reason="runtime approval registry 中没有匹配的批准记录",
            risk_level=contract.risk_level,
        )
        _log_contract_event(thread_id, "contract_violation", decision, tool_name, args)
        return decision

    decisions = [check_tool_allowed(contract, tool_name)]
    boundary_decision = check_resource_boundary(contract, resource)
    if boundary_decision is not None:
        decisions.append(boundary_decision)

    if capability == "read":
        decision = check_read_path(contract, resource or str(args.get("filepath", args.get("sub_dir", ""))))
    elif capability == "write":
        decision = check_write_path(contract, resource or str(args.get("filepath", "")))
    elif capability == "execute":
        command = "check:" + str(args.get("check_id", "")) if tool_name == "run_office_check" else str(args.get("command", ""))
        decision = check_shell_command(contract, command)
    elif tool_name == "write_office_file":
        decision = check_write_path(contract, str(args.get("filepath", "")))
    elif tool_name == "execute_office_shell":
        decision = check_shell_command(contract, str(args.get("command", "")))
    else:
        decision = allow(contract, "tool_policy.allowed_tools", f"工具 {tool_name} 通过通用契约校验")

    decisions.append(decision)

    if tool_name in contract.tool_policy.high_risk_tools:
        decision = require_confirmation(
            contract,
            "tool_policy.high_risk_tools",
            f"工具 {tool_name} 被契约标记为高风险工具",
        )
        decisions.append(decision)

    if tool_name in contract.tool_policy.require_confirmation_for and effect != "read":
        decision = require_confirmation(
            contract,
            "tool_policy.require_confirmation_for",
            f"工具 {tool_name} 属于需要人工确认的高风险工具",
        )
        decisions.append(decision)

    priority = {"allow": 0, "require_confirmation": 1, "deny": 2}
    decision = max(decisions, key=lambda item: priority[item.decision]).model_copy(deep=True)
    decision.matched_clauses = list(dict.fromkeys(item.clause for item in decisions if item.clause))
    event = {"allow": "contract_check", "deny": "contract_violation",
             "require_confirmation": "contract_confirmation_required"}[decision.decision]
    _log_contract_event(thread_id, event, decision, tool_name, args)
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
