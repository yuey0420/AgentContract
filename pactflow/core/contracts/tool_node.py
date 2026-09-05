import json
import re
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

from ..approval import ApprovalService
from ..logger import audit_logger
from ..execution import ExecutionContext, current_execution
from ..runtime_store import runtime_store
from .guard import format_contract_denial, guard_tool_call
from .action_risk import classify_shell_effect
from .instructions import InstructionBuilder, InstructionEnvelope, classify_tool_result
from .models import ContractDecision, TaskContract
from .security_policy import PolicyEvaluation, SecurityPolicyRuntime
from .store import load_active_contract, compute_contract_hash


@dataclass(frozen=True)
class ToolIntent:
    capability: str
    resource: str | None = None


_TOOL_INTENTS: dict[str, tuple[str, str | None]] = {
    "get_current_time": ("pure", None),
    "calculator": ("pure", None),
    "get_system_model_info": ("pure", None),
    "list_office_files": ("read", "sub_dir"),
    "read_office_file": ("read", "filepath"),
    "write_office_file": ("write", "filepath"),
    "patch_office_file": ("write", "filepath"),
    "run_office_check": ("execute", "check_id"),
    "execute_office_shell": ("execute", "command"),
    "save_user_profile": ("write", None),
    "read_user_profile": ("read", None),
    "schedule_task": ("write", None),
    "list_scheduled_tasks": ("read", None),
    "delete_scheduled_task": ("write", None),
    "modify_scheduled_task": ("write", None),
}

_FIXED_RESOURCES = {
    "save_user_profile": "memory/user_profile.md",
    "read_user_profile": "memory/user_profile.md",
    "schedule_task": "runtime/scheduled_tasks",
    "list_scheduled_tasks": "runtime/scheduled_tasks",
    "delete_scheduled_task": "runtime/scheduled_tasks",
    "modify_scheduled_task": "runtime/scheduled_tasks",
}


def resolve_tool_intent(tool: BaseTool, args: dict[str, Any]) -> ToolIntent:
    metadata = getattr(tool, "metadata", None) or {}
    capability = str(metadata.get("capability") or _TOOL_INTENTS.get(tool.name, ("external", None))[0])
    path_arg = metadata.get("resource_arg") or _TOOL_INTENTS.get(tool.name, ("external", None))[1]
    resource = metadata.get("resource") or _FIXED_RESOURCES.get(tool.name)
    if args.get("mode") in {"manifest", "help"} and metadata.get("help_resource"):
        return ToolIntent(capability="read", resource=str(metadata["help_resource"]))
    if path_arg:
        resource = str(args.get(str(path_arg), ""))
    return ToolIntent(capability=capability, resource=resource)


def _result_content(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except TypeError:
        return str(result)


def _result_indicates_failure(content: str) -> bool:
    try:
        structured = json.loads(content)
        if isinstance(structured, dict):
            if structured.get("exit_code") not in {None, 0}:
                return True
            if "status" in structured:
                return structured["status"] not in {"succeeded", "passed"}
    except (ValueError, TypeError):
        pass
    exit_code = re.search(r"Exit Code\)?\s*:\s*(-?\d+)", content, re.IGNORECASE)
    if exit_code and int(exit_code.group(1)) != 0:
        return True
    normalized = content.lstrip()
    failure_prefixes = (
        "❌", "错误", "失败", "计算出错", "设定失败", "删除失败",
        "修改失败", "查询失败", "操作异常", "文件不存在", "目录不存在",
        "越权拦截", "契约拒绝",
    )
    return normalized.startswith(failure_prefixes)


class ContractToolNode:
    """Single policy and audit gateway for every Agent-visible tool."""

    def __init__(
        self,
        tools: Iterable[BaseTool],
        security_runtime: SecurityPolicyRuntime | None = None,
    ):
        self.tools = {tool.name: tool for tool in tools}
        self.security_runtime = security_runtime or SecurityPolicyRuntime.from_environment()
        self.instruction_builder = InstructionBuilder(self.security_runtime.mode)

    def __call__(self, state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        messages = state.get("messages", [])
        if not messages:
            raise ValueError("ContractToolNode requires at least one message")

        last_message = messages[-1]
        tool_calls = getattr(last_message, "tool_calls", None) or []
        thread_id = config.get("configurable", {}).get("thread_id", "system_default")
        run_id = state.get("run_id")
        execution_mode = state.get("execution_mode", "chat")
        process_profile = state.get("process_profile", "chat")
        plans: list[dict[str, Any]] = []
        approvals = ApprovalService(runtime_store)
        process_run = runtime_store.get_process_run(run_id) if run_id else None

        for call in tool_calls:
            tool_name = call.get("name", "")
            args = dict(call.get("args") or {})
            tool_call_id = call.get("id", "")
            tool = self.tools.get(tool_name)

            if tool is None:
                content = f"工具不存在：{tool_name}"
                audit_logger.log_event(
                    thread_id=thread_id,
                    event="tool_failed",
                    run_id=run_id,
                    tool=tool_name,
                    error=content,
                )
                if run_id:
                    runtime_store.append_run_event(run_id, "tool_failed", {
                        "tool": tool_name,
                        "error": content,
                    })
                plans.append({"tool_name": tool_name, "tool_call_id": tool_call_id, "content": content})
                continue

            intent = resolve_tool_intent(tool, args)
            if intent.capability in {"read", "write"} and intent.resource:
                from pathlib import PurePosixPath
                intent = ToolIntent(intent.capability, str(PurePosixPath(intent.resource.replace("\\", "/"))))
            effect = (
                classify_shell_effect(str(args.get("command", "")))
                if tool_name == "execute_office_shell"
                else intent.capability
            )
            contract: TaskContract | None = None
            try:
                contract = load_active_contract()
            except Exception:
                pass
            snapshot_changed = False
            if process_run:
                snapshot = process_run.get("contract")
                pinned = TaskContract.model_validate(snapshot) if snapshot else None
                snapshot_changed = (
                    (compute_contract_hash(contract) if contract else None)
                    != (compute_contract_hash(pinned) if pinned else None)
                )
                contract = pinned
            execution = None
            if run_id:
                execution = runtime_store.register_execution(
                    run_id, tool_call_id, tool_name, args,
                    compute_contract_hash(contract) if contract else None,
                    intent.capability, intent.resource,
                )
                if execution["status"] in {"succeeded", "failed", "denied", "outcome_unknown"}:
                    saved = json.loads(execution["result_json"])
                    plans.append({"tool_name": tool_name, "tool_call_id": tool_call_id, "content": saved["content"], "saved_security": saved.get("security", {})})
                    continue
                if execution["status"] == "started":
                    content = "执行结果未知：动作已启动但未保存结果，必须先核对实际产物。"
                    runtime_store.finish_execution(run_id, tool_call_id, "outcome_unknown", {"content": content}, "execution.outcome_unknown", {"tool_call_id": tool_call_id})
                    if process_run:
                        runtime_store.transition_run(run_id, "waiting", waiting={"reason": "outcome_unknown", "blocked_node_ids": [], "resume_target": "verifying", "tool_call_id": tool_call_id})
                    plans.append({"tool_name": tool_name, "tool_call_id": tool_call_id, "content": content})
                    continue

            raw_references = call.get("reference_tool_ids") or []
            explicit_references = (
                [raw_references] if isinstance(raw_references, str) else list(raw_references)
            )
            if call.get("reference_tool_id"):
                explicit_references.append(str(call["reference_tool_id"]))
            instruction = self.instruction_builder.build(
                tool=tool,
                args=args,
                capability=intent.capability,
                resource=intent.resource,
                messages=messages[:-1],
                run_id=run_id,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                explicit_reference_tool_ids=explicit_references,
            )
            security_evaluation = self.security_runtime.check(instruction)
            source_fingerprint = hashlib.sha256(json.dumps({
                "instruction": instruction.model_dump(), "policy": security_evaluation.model_dump(),
                "sources": [{"id": getattr(message, "tool_call_id", None), "content": message.content}
                            for message in messages[:-1] if getattr(message, "tool_call_id", None) in instruction.reference_tool_ids],
            }, sort_keys=True, default=str).encode()).hexdigest()
            policy_changed = run_id and not runtime_store.bind_execution_policy(run_id, tool_call_id, source_fingerprint)

            if run_id:
                runtime_store.record_instruction(instruction.model_dump())
                prior_events = runtime_store.get_run_events(run_id)
                if not any(
                    event.get("event") == "tool_requested"
                    and event.get("tool_call_id") == tool_call_id
                    for event in prior_events
                ):
                    runtime_store.append_run_event(run_id, "tool_requested", {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "instruction_id": instruction.instruction_id,
                        "capability": intent.capability,
                        "resource": intent.resource,
                        "reference_tool_ids": instruction.reference_tool_ids,
                        "trustworthiness": instruction.trustworthiness,
                        "confidentiality": instruction.confidentiality,
                        "risk_level": instruction.risk_level,
                    })
                if intent.capability in {"write", "execute", "external"}:
                    execution_mode = "managed_task" if contract else "guarded_action"
                    runtime_store.update_run_mode(run_id, execution_mode)

            self._record_security_evaluation(instruction, security_evaluation)
            decision = guard_tool_call(
                thread_id, tool_name, args, capability=intent.capability,
                resource=intent.resource, effect=effect, contract_snapshot=contract,
            )
            if contract and run_id:
                limit_decision = self._check_limits(contract, run_id, args, intent, decision, tool_call_id)
                if decision.decision != "deny":
                    decision = limit_decision
            if security_evaluation.effective_decision == "deny" or (decision.decision != "deny" and security_evaluation.effective_decision == "require_confirmation"):
                decision = self._security_decision(instruction, security_evaluation, contract_decision=decision)

            if policy_changed:
                decision = ContractDecision(decision="deny", clause="approval.source_or_policy_changed", reason="动作的数据来源或策略已变化，需要重新提交并授权。")
            if snapshot_changed or (process_run and (process_run["run_status"] == "finished" or (process_run.get("waiting") or {}).get("reason") in {"user_pause", "feedback_requires_revision"})):
                decision = ContractDecision(decision="deny", clause="contract.snapshot_changed", reason="运行契约已变化或运行已结束，需要后继运行。")

            authorization_ref = None
            grant_ref = None
            if decision.decision != "deny" and tool_name in {"write_office_file", "patch_office_file"} and args.get("mode") != "a":
                from ..tools.sandbox_tools import _get_safe_path
                from ..process.acceptance_surface import acceptance_semantics_changed
                from pathlib import Path
                try:
                    target = Path(_get_safe_path(str(args.get("filepath", ""))))
                    if target.is_file() and acceptance_semantics_changed(str(args["filepath"]), target.read_text(encoding="utf-8"), str(args.get("content", ""))):
                        decision = ContractDecision(decision="require_confirmation", contract_id=decision.contract_id,
                            contract_hash=decision.contract_hash, clause="acceptance.semantic_change",
                            reason="现有测试断言、跳过标记或 mock 范围发生变化，需要复核验收语义。", risk_level="high",
                            matched_clauses=decision.matched_clauses + ["acceptance.semantic_change"])
                except (OSError, ValueError):
                    decision = ContractDecision(decision="deny", clause="scope.path_safety", reason="无法核验目标文件。")

            if decision.decision == "require_confirmation" and run_id:
                grant = None
                if self._scope_eligible(
                    decision, instruction, process_profile, effect
                ):
                    from ..process_storage import now
                    grant = next((item for item in runtime_store.list_approval_grants(run_id)
                                  if item["thread_id"] == thread_id
                                  and item["contract_hash"] == decision.contract_hash
                                  and item["tool_name"] == tool_name and item["capability"] == intent.capability
                                  and item["resource_pattern"] == (intent.resource or "").replace("\\", "/")
                                  and not item["revoked_at"] and item["expires_at"] > now()
                                  and (item["uses"] < item["max_uses"] or execution.get("authorization_ref") == item["grant_id"])), None)
                if grant:
                    grant_ref = grant["grant_id"]
                    runtime_store.update_instruction(
                        instruction.instruction_id,
                        status="approved",
                        policy_decision="allow",
                    )
                    decision = ContractDecision(
                        decision="allow",
                        contract_id=decision.contract_id,
                        contract_hash=decision.contract_hash,
                        clause="approval.scoped_grant",
                        reason="匹配的运行级范围授权已原子消费",
                        risk_level=decision.risk_level,
                    )

            if decision.decision == "require_confirmation" and run_id:
                runtime_store.update_instruction(
                    instruction.instruction_id,
                    status="awaiting_approval",
                    policy_decision=decision.decision,
                )
                risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
                approval_risk = max(
                    [instruction.risk_level, decision.risk_level or "high"],
                    key=lambda value: risk_order.get(value, 2),
                )
                action = approvals.request(
                    run_id=run_id,
                    thread_id=thread_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=args,
                    contract_hash=decision.contract_hash,
                    risk_level=approval_risk,
                    clause=decision.clause,
                    reason=decision.reason,
                    capability=intent.capability,
                    resource=intent.resource,
                    process_profile=process_profile,
                    effect=effect,
                )
                scope_eligible = self._scope_eligible(
                    decision, instruction, process_profile, effect
                )
                if process_run:
                    runtime_store.transition_run(run_id, "waiting", waiting={"reason": "exact_action", "blocked_node_ids": [], "resume_target": "executing", "action_id": action["action_id"]})
                resolution = interrupt({
                    "type": "approval_required",
                    "action_id": action["action_id"],
                    "tool": tool_name,
                    "arguments": action.get("args", {}),
                    "risk_level": action.get("risk_level") or "high",
                    "contract_id": decision.contract_id,
                    "clause": decision.clause,
                    "reason": decision.reason,
                    "expires_at": action.get("expires_at"),
                    "scope": {"resource": intent.resource, "tool": tool_name, "ttl_minutes": 15,
                              "max_uses": 5 if process_profile == "audited" else 20,
                              "risk_ceiling": "medium", "excluded": ["execute", "external", "destructive", "source_confirmation"]} if scope_eligible else None,
                    "approval_options": (
                        ["once", "run_scope", "reject"]
                        if scope_eligible else ["once", "reject"]
                    ),
                })
                resumed_action_id = (
                    resolution.get("action_id") if isinstance(resolution, dict) else None
                )
                if resumed_action_id != action["action_id"]:
                    raise ValueError("Approval resume token does not match the interrupted action")
                if process_run:
                    live_process = runtime_store.get_process_run(run_id)
                    if live_process["run_status"] == "finished" or (live_process.get("waiting") or {}).get("reason") in {"user_pause", "feedback_requires_revision"}:
                        plans.append({"tool_name": tool_name, "tool_call_id": tool_call_id, "content": "操作未执行：运行已取消或目标修订尚未完成。"})
                        continue
                    runtime_store.transition_run(run_id, "executing")

                approval = approvals.get(action["action_id"])
                if approval.status == "approved" or (approval.status == "consumed" and execution["status"] == "authorized" and execution["authorization_ref"] == action["action_id"]):
                    authorization_ref = action["action_id"]
                    runtime_store.update_instruction(
                        instruction.instruction_id,
                        status="approved",
                        policy_decision="allow",
                    )
                    decision = ContractDecision(
                        decision="allow",
                        contract_id=decision.contract_id,
                        contract_hash=decision.contract_hash,
                        clause="approval.one_time",
                        reason="原始工具调用的一次性人工批准已精确消费",
                        risk_level=decision.risk_level,
                    )
                else:
                    status = approval.status
                    runtime_store.update_instruction(
                        instruction.instruction_id,
                        status="denied",
                        policy_decision="deny",
                    )
                    runtime_store.append_run_event(run_id, "tool_denied", {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "capability": intent.capability,
                        "resource": intent.resource,
                        "clause": f"approval.{status}",
                        "reason": f"人工审批状态为 {status}，原始工具调用未执行",
                    })
                    plans.append({
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "instruction": instruction,
                        "content": f"操作未执行：人工审批状态为 {status}。",
                    })
                    continue

            if decision.decision == "allow" and run_id:
                reservation_error = runtime_store.authorize_execution(
                    run_id, tool_call_id, contract.limits if contract else None,
                    approval_id=authorization_ref, grant_id=grant_ref,
                )
                if reservation_error:
                    decision = ContractDecision(decision="deny", clause=reservation_error, reason="动作授权或硬预算预留失败。")

            if decision.decision != "allow":
                if run_id:
                    runtime_store.update_instruction(
                        instruction.instruction_id,
                        status="denied",
                        policy_decision=decision.decision,
                    )
                    runtime_store.append_run_event(run_id, "tool_denied", {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "instruction_id": instruction.instruction_id,
                        "capability": intent.capability,
                        "resource": intent.resource,
                        "clause": decision.clause,
                        "reason": decision.reason,
                    })
                    runtime_store.finish_execution(run_id, tool_call_id, "denied", {"content": self._format_denial(decision)})
                plans.append({
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "instruction": instruction,
                    "content": self._format_denial(decision),
                })
                continue

            if run_id:
                runtime_store.update_instruction(
                    instruction.instruction_id,
                    status="approved",
                    policy_decision="allow",
                )
            plans.append({
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "tool": tool,
                "args": args,
                "intent": intent,
                "contract": contract,
                "instruction": instruction,
            })

        outputs: list[ToolMessage] = []
        for plan in plans:
            tool_name = plan["tool_name"]
            tool_call_id = plan["tool_call_id"]
            instruction = plan.get("instruction")
            if "content" in plan:
                outputs.append(ToolMessage(
                    content=plan["content"],
                    tool_call_id=tool_call_id,
                    name=tool_name,
                    additional_kwargs={
                        "security": self._message_security(instruction)
                    } if instruction else {"security": plan.get("saved_security", {"trustworthiness": "unknown"})},
                ))
                continue

            tool = plan["tool"]
            args = plan["args"]
            intent = plan["intent"]
            contract = plan["contract"]
            if run_id:
                if not self._authorization_still_valid(run_id, tool_call_id, contract, process_run):
                    content = "操作未执行：执行前授权已过期、撤销或契约已变化。"
                    runtime_store.finish_execution(run_id, tool_call_id, "denied", {"content": content}, "tool_denied", {"tool": tool_name, "tool_call_id": tool_call_id})
                    outputs.append(ToolMessage(content=content, tool_call_id=tool_call_id, name=tool_name))
                    continue
                if not runtime_store.start_execution(run_id, tool_call_id):
                    raise RuntimeError("Action is already started or no longer authorized")
            result_trust, result_confidentiality = classify_tool_result(
                tool, intent.capability, args
            )
            audit_logger.log_event(
                thread_id=thread_id,
                event="tool_started",
                run_id=run_id,
                tool=tool_name,
                capability=intent.capability,
                resource=intent.resource,
            )
            try:
                timeout = contract.limits.max_shell_seconds if contract else 60
                token = current_execution.set(ExecutionContext(
                    thread_id=thread_id,
                    run_id=run_id,
                    shell_timeout=timeout,
                    checks=contract.inputs.get("checks", {}) if contract else {},
                ))
                try:
                    result = tool.invoke(args, config=config)
                finally:
                    current_execution.reset(token)
                content = _result_content(result)
                result_event = "tool_failed" if _result_indicates_failure(content) else "tool_succeeded"
                if run_id:
                    runtime_store.update_instruction(
                        instruction.instruction_id,
                        status="failed" if result_event == "tool_failed" else "succeeded",
                        result_trustworthiness=result_trust,
                        result_confidentiality=result_confidentiality,
                    )
                    actual_status = result.get("status") if isinstance(result, dict) else None
                    runtime_store.finish_execution(run_id, tool_call_id,
                        "outcome_unknown" if actual_status == "outcome_unknown" else ("failed" if result_event == "tool_failed" else "succeeded"),
                        {"content": content, "security": self._message_security(instruction, trustworthiness=result_trust, confidentiality=result_confidentiality)}, result_event, {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "instruction_id": instruction.instruction_id,
                        "capability": intent.capability,
                        "resource": intent.resource,
                        "result": result if isinstance(result, dict) else None,
                    })
                    if actual_status == "outcome_unknown":
                        runtime_store.append_run_event(run_id, "execution.outcome_unknown", {"tool_call_id": tool_call_id})
                    if result_event == "tool_succeeded" and intent.capability == "write" and process_run:
                        revision = runtime_store.get_record("plan", run_id)
                        if revision and intent.resource not in revision["payload"].get("predicted_files", []):
                            updated = dict(revision["payload"])
                            updated["predicted_files"] = updated.get("predicted_files", []) + [intent.resource]
                            runtime_store.revise_record("plan", run_id, updated, expected_revision=revision["revision"], run_id=run_id)
                audit_logger.log_event(
                    thread_id=thread_id,
                    event=result_event,
                    run_id=run_id,
                    tool=tool_name,
                    capability=intent.capability,
                    resource=intent.resource,
                    result_summary=content[:200],
                )
            except Exception as exc:
                result_trust = "unknown"
                content = f"工具执行失败：{exc}"
                if run_id:
                    runtime_store.update_instruction(
                        instruction.instruction_id,
                        status="failed",
                        result_trustworthiness="unknown",
                        result_confidentiality=result_confidentiality,
                    )
                    runtime_store.finish_execution(run_id, tool_call_id, "failed", {"content": content}, "tool_failed", {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "instruction_id": instruction.instruction_id,
                        "capability": intent.capability,
                        "resource": intent.resource,
                        "error": str(exc),
                    })
                audit_logger.log_event(
                    thread_id=thread_id,
                    event="tool_failed",
                    run_id=run_id,
                    tool=tool_name,
                    capability=intent.capability,
                    resource=intent.resource,
                    error=str(exc),
                )

            outputs.append(ToolMessage(
                content=content,
                tool_call_id=tool_call_id,
                name=tool_name,
                additional_kwargs={"security": self._message_security(
                    instruction,
                    trustworthiness=result_trust,
                    confidentiality=result_confidentiality,
                )},
            ))

        return {
            "messages": outputs,
            "execution_mode": execution_mode,
            "process_phase": "execute",
        }

    @staticmethod
    def _message_security(
        instruction: InstructionEnvelope,
        *,
        trustworthiness: str | None = None,
        confidentiality: str | None = None,
    ) -> dict[str, Any]:
        return {
            "instruction_id": instruction.instruction_id,
            "tool_call_id": instruction.tool_call_id,
            "reference_tool_ids": instruction.reference_tool_ids,
            "trustworthiness": trustworthiness or instruction.trustworthiness,
            "confidentiality": confidentiality or instruction.confidentiality,
            "risk_level": instruction.risk_level,
        }

    @staticmethod
    def _format_denial(decision: ContractDecision) -> str:
        if (decision.clause or "").startswith("security."):
            return (
                "安全策略拒绝：本次工具调用未执行。\n"
                f"策略：{decision.clause}\n"
                f"原因：{decision.reason}"
            )
        return format_contract_denial(decision)

    @staticmethod
    def _security_decision(
        instruction: InstructionEnvelope,
        evaluation: PolicyEvaluation,
        *,
        contract_decision: ContractDecision | None = None,
    ) -> ContractDecision:
        finding = evaluation.primary_finding
        return ContractDecision(
            decision=evaluation.effective_decision,
            contract_id=contract_decision.contract_id if contract_decision else None,
            contract_hash=contract_decision.contract_hash if contract_decision else None,
            clause=finding.policy if finding else "security.policy",
            reason=finding.reason if finding else "Security policy requires review.",
            risk_level=instruction.risk_level,
            matched_clauses=list(dict.fromkeys((contract_decision.matched_clauses if contract_decision else []) + [item.policy for item in evaluation.findings])),
        )

    @staticmethod
    def _record_security_evaluation(
        instruction: InstructionEnvelope,
        evaluation: PolicyEvaluation,
    ):
        if not evaluation.findings:
            return
        finding = evaluation.primary_finding
        event = (
            "security_policy_observed"
            if evaluation.mode == "observe"
            else (
                "security_policy_denied"
                if evaluation.effective_decision == "deny"
                else "security_confirmation_required"
            )
        )
        payload = {
            "instruction_id": instruction.instruction_id,
            "tool": instruction.tool_name,
            "tool_call_id": instruction.tool_call_id,
            "decision": evaluation.decision,
            "effective_decision": evaluation.effective_decision,
            "mode": evaluation.mode,
            "policy": finding.policy if finding else None,
            "reason": finding.reason if finding else None,
            "findings": [item.model_dump() for item in evaluation.findings],
            "reference_tool_ids": instruction.reference_tool_ids,
            "trustworthiness": instruction.trustworthiness,
            "confidentiality": instruction.confidentiality,
        }
        if instruction.run_id:
            prior_events = runtime_store.get_run_events(instruction.run_id)
            if any(
                prior.get("event") == event
                and prior.get("instruction_id") == instruction.instruction_id
                for prior in prior_events
            ):
                return
            runtime_store.append_run_event(instruction.run_id, event, payload)
        audit_logger.log_event(
            thread_id=instruction.thread_id,
            event=event,
            run_id=instruction.run_id,
            **payload,
        )

    @staticmethod
    def _check_limits(
        contract: TaskContract,
        run_id: str,
        args: dict[str, Any],
        intent: ToolIntent,
        allowed: ContractDecision,
        tool_call_id: str = "",
    ) -> ContractDecision:
        events = runtime_store.get_run_events(run_id)
        limits = contract.limits
        budget_error = runtime_store.execution_budget(run_id, tool_call_id, intent.capability, intent.resource, limits)
        if budget_error:
            return ContractDecision(
                decision="deny",
                contract_id=contract.id,
                contract_hash=allowed.contract_hash,
                clause=budget_error,
                reason="动作将超过本轮硬预算",
                risk_level=contract.risk_level,
            )

        if intent.capability == "write" and limits.max_file_bytes is not None:
            content = args.get("content") or args.get("new_content")
            if isinstance(content, str) and len(content.encode("utf-8")) > limits.max_file_bytes:
                return ContractDecision(
                    decision="deny",
                    contract_id=contract.id,
                    contract_hash=allowed.contract_hash,
                    clause="limits.max_file_bytes",
                    reason=f"单次写入超过 {limits.max_file_bytes} 字节上限",
                    risk_level=contract.risk_level,
                )

        if intent.capability == "write" and limits.max_written_files is not None:
            written = {
                event.get("resource")
                for event in events
                if event.get("event") == "tool_succeeded" and event.get("capability") == "write"
            }
            if intent.resource not in written and len(written) >= limits.max_written_files:
                return ContractDecision(
                    decision="deny",
                    contract_id=contract.id,
                    contract_hash=allowed.contract_hash,
                    clause="limits.max_written_files",
                    reason=f"写入资源数量达到 {limits.max_written_files} 个上限",
                    risk_level=contract.risk_level,
                )
        return allowed

    @staticmethod
    def _scope_eligible(
        decision: ContractDecision,
        instruction: InstructionEnvelope,
        process_profile: str,
        effect: str,
    ) -> bool:
        clauses = decision.matched_clauses or [decision.clause]
        if any(clause and (clause.startswith("security.") or clause in {"scope.human_only", "tool_policy.high_risk_tools"}) for clause in clauses):
            return False
        return ApprovalService.scope_eligible({
            "contract_hash": decision.contract_hash, "process_profile": process_profile,
            "risk_level": max([instruction.risk_level, decision.risk_level or "low"], key=lambda r: {"low": 0, "medium": 1, "high": 2, "critical": 3}[r]),
            "capability": instruction.capability, "effect": effect,
            "clause": decision.clause, "tool_name": instruction.tool_name,
        })

    @staticmethod
    def _authorization_still_valid(run_id, call_id, contract, process_run):
        from ..process_storage import now
        if process_run:
            try:
                current = load_active_contract()
                if (compute_contract_hash(current) if current else None) != (compute_contract_hash(contract) if contract else None):
                    return False
            except Exception:
                return False
            current_run = runtime_store.get_process_run(run_id)
            if current_run["run_status"] == "finished" or (current_run.get("waiting") or {}).get("reason") in {"user_pause", "feedback_requires_revision"}:
                return False
        with runtime_store._connect() as conn:
            row = conn.execute("SELECT authorization_ref FROM action_executions WHERE run_id=? AND tool_call_id=?", (run_id, call_id)).fetchone()
        if row and row[0]:
            action = runtime_store.get_action(row[0])
            if action:
                return action["status"] == "consumed" and action["expires_at"] > now()
            grant = runtime_store.get_approval_grant(row[0])
            return bool(grant and not grant["revoked_at"] and grant["expires_at"] > now())
        return True
