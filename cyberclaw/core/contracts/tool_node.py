import json
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
from .models import ContractDecision, TaskContract
from .store import load_active_contract


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
    if args.get("mode") == "help" and metadata.get("help_resource"):
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
    normalized = content.lstrip()
    failure_prefixes = (
        "❌", "错误", "失败", "计算出错", "设定失败", "删除失败",
        "修改失败", "查询失败", "操作异常", "文件不存在", "目录不存在",
        "越权拦截", "契约拒绝",
    )
    return normalized.startswith(failure_prefixes)


class ContractToolNode:
    """Single policy and audit gateway for every Agent-visible tool."""

    def __init__(self, tools: Iterable[BaseTool]):
        self.tools = {tool.name: tool for tool in tools}

    def __call__(self, state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        messages = state.get("messages", [])
        if not messages:
            raise ValueError("ContractToolNode requires at least one message")

        last_message = messages[-1]
        tool_calls = getattr(last_message, "tool_calls", None) or []
        thread_id = config.get("configurable", {}).get("thread_id", "system_default")
        run_id = state.get("run_id")
        execution_mode = state.get("execution_mode", "chat")
        plans: list[dict[str, Any]] = []
        approvals = ApprovalService(runtime_store)

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
            contract: TaskContract | None = None
            try:
                contract = load_active_contract()
            except Exception:
                pass

            if run_id:
                prior_events = runtime_store.get_run_events(run_id)
                if not any(
                    event.get("event") == "tool_requested"
                    and event.get("tool_call_id") == tool_call_id
                    for event in prior_events
                ):
                    runtime_store.append_run_event(run_id, "tool_requested", {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "capability": intent.capability,
                        "resource": intent.resource,
                    })
                if intent.capability in {"write", "execute", "external"}:
                    execution_mode = "managed_task" if contract else "guarded_action"
                    runtime_store.update_run_mode(run_id, execution_mode)

            decision = guard_tool_call(
                thread_id,
                tool_name,
                args,
                capability=intent.capability,
                resource=intent.resource,
            )

            if decision.decision == "allow" and contract and run_id:
                decision = self._check_limits(contract, run_id, args, intent, decision)

            if decision.decision == "require_confirmation" and run_id:
                action = approvals.request(
                    run_id=run_id,
                    thread_id=thread_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=args,
                    contract_hash=decision.contract_hash,
                    risk_level=decision.risk_level,
                    clause=decision.clause,
                    reason=decision.reason,
                )
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
                })
                resumed_action_id = (
                    resolution.get("action_id") if isinstance(resolution, dict) else None
                )
                if resumed_action_id != action["action_id"]:
                    raise ValueError("Approval resume token does not match the interrupted action")

                approval = approvals.consume_exact(
                    action["action_id"],
                    thread_id=thread_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=args,
                    contract_hash=decision.contract_hash,
                )
                if approval.status == "consumed":
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
                        "content": f"操作未执行：人工审批状态为 {status}。",
                    })
                    continue

            if decision.decision != "allow":
                if run_id:
                    runtime_store.append_run_event(run_id, "tool_denied", {
                        "tool": tool_name,
                        "capability": intent.capability,
                        "resource": intent.resource,
                        "clause": decision.clause,
                        "reason": decision.reason,
                    })
                plans.append({
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "content": format_contract_denial(decision),
                })
                continue

            plans.append({
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "tool": tool,
                "args": args,
                "intent": intent,
                "contract": contract,
            })

        outputs: list[ToolMessage] = []
        for plan in plans:
            tool_name = plan["tool_name"]
            tool_call_id = plan["tool_call_id"]
            if "content" in plan:
                outputs.append(ToolMessage(
                    content=plan["content"],
                    tool_call_id=tool_call_id,
                    name=tool_name,
                ))
                continue

            tool = plan["tool"]
            args = plan["args"]
            intent = plan["intent"]
            contract = plan["contract"]
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
                ))
                try:
                    result = tool.invoke(args, config=config)
                finally:
                    current_execution.reset(token)
                content = _result_content(result)
                result_event = "tool_failed" if _result_indicates_failure(content) else "tool_succeeded"
                if run_id:
                    runtime_store.append_run_event(run_id, result_event, {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
                        "capability": intent.capability,
                        "resource": intent.resource,
                    })
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
                content = f"工具执行失败：{exc}"
                if run_id:
                    runtime_store.append_run_event(run_id, "tool_failed", {
                        "tool": tool_name,
                        "tool_call_id": tool_call_id,
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

            outputs.append(ToolMessage(content=content, tool_call_id=tool_call_id, name=tool_name))

        return {
            "messages": outputs,
            "execution_mode": execution_mode,
            "process_phase": "execute",
        }

    @staticmethod
    def _check_limits(
        contract: TaskContract,
        run_id: str,
        args: dict[str, Any],
        intent: ToolIntent,
        allowed: ContractDecision,
    ) -> ContractDecision:
        events = runtime_store.get_run_events(run_id)
        limits = contract.limits
        requested = sum(event.get("event") == "tool_requested" for event in events)
        if limits.max_tool_calls is not None and requested > limits.max_tool_calls:
            return ContractDecision(
                decision="deny",
                contract_id=contract.id,
                contract_hash=allowed.contract_hash,
                clause="limits.max_tool_calls",
                reason=f"工具调用次数 {requested} 超过上限 {limits.max_tool_calls}",
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
