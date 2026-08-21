import hashlib
import json
from collections.abc import Iterable
from typing import Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from .action_risk import classify_shell_effect, shell_risk_level


Trustworthiness = Literal["trusted", "unknown", "untrusted"]
Confidentiality = Literal["public", "internal", "confidential", "restricted"]
GovernanceMode = Literal["enforce", "observe"]
InstructionAuthority = Literal["model", "contract", "human", "system"]


class InstructionEnvelope(BaseModel):
    """Normalized, auditable representation of one requested tool action."""

    model_config = ConfigDict(extra="forbid")

    instruction_id: str
    run_id: str | None = None
    thread_id: str
    tool_call_id: str
    parent_instruction_id: str | None = None
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    capability: str
    resource: str | None = None
    reference_tool_ids: list[str] = Field(default_factory=list)
    trustworthiness: Trustworthiness = "trusted"
    confidentiality: Confidentiality = "public"
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    authority: InstructionAuthority = "model"
    governance_mode: GovernanceMode = "enforce"


_TRUST_ORDER = {"trusted": 0, "unknown": 1, "untrusted": 2}
_CONFIDENTIALITY_ORDER = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_DEFAULT_RISK = {
    "pure": "low",
    "read": "low",
    "write": "medium",
    "execute": "high",
    "external": "high",
}


def _highest(values: Iterable[str], order: dict[str, int], default: str) -> str:
    valid = [value for value in values if value in order]
    return max(valid, key=order.__getitem__) if valid else default


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for nested in value.values():
            strings.extend(_flatten_strings(nested))
        return strings
    if isinstance(value, (list, tuple, set)):
        strings = []
        for nested in value:
            strings.extend(_flatten_strings(nested))
        return strings
    return []


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _directly_references(args: dict[str, Any], content: str) -> bool:
    normalized_content = content.strip().casefold()
    if not normalized_content:
        return False
    for value in _flatten_strings(args):
        normalized_value = value.strip().casefold()
        if len(normalized_value) < 4:
            continue
        if normalized_value in normalized_content or normalized_content in normalized_value:
            return True
    return False


def classify_tool_result(
    tool: BaseTool,
    capability: str,
    args: dict[str, Any] | None = None,
) -> tuple[Trustworthiness, Confidentiality]:
    metadata = getattr(tool, "metadata", None) or {}
    trust = metadata.get("result_trustworthiness")
    confidentiality = metadata.get("result_confidentiality")

    if trust not in _TRUST_ORDER:
        if tool.name in {
            "list_office_files", "read_office_file", "read_user_profile",
            "list_scheduled_tasks",
        }:
            trust = "trusted"
        elif (
            tool.name == "execute_office_shell"
            and classify_shell_effect(str((args or {}).get("command", ""))) == "read"
        ):
            trust = "trusted"
        else:
            trust = {
                "pure": "trusted",
                "read": "unknown",
                "write": "trusted",
                "execute": "unknown",
                "external": "untrusted",
            }.get(capability, "unknown")
    if confidentiality not in _CONFIDENTIALITY_ORDER:
        if tool.name == "read_user_profile":
            confidentiality = "confidential"
        elif capability == "read":
            confidentiality = "internal"
        else:
            confidentiality = "public"
    return trust, confidentiality


class InstructionBuilder:
    def __init__(self, governance_mode: GovernanceMode = "enforce"):
        self.governance_mode = governance_mode

    def build(
        self,
        *,
        tool: BaseTool,
        args: dict[str, Any],
        capability: str,
        resource: str | None,
        messages: list[Any],
        run_id: str | None,
        thread_id: str,
        tool_call_id: str,
        explicit_reference_tool_ids: Iterable[str] = (),
    ) -> InstructionEnvelope:
        metadata = getattr(tool, "metadata", None) or {}
        source_messages: list[ToolMessage] = []
        explicit_references = {str(item) for item in explicit_reference_tool_ids if item}

        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            source_id = str(getattr(message, "tool_call_id", "") or "")
            if not source_id:
                continue
            if source_id in explicit_references or _directly_references(
                args, _content_text(message.content)
            ):
                source_messages.append(message)

        references = sorted({
            str(message.tool_call_id)
            for message in source_messages
            if getattr(message, "tool_call_id", None)
        } | explicit_references)
        source_security = [
            (message.additional_kwargs or {}).get("security", {})
            for message in source_messages
        ]
        trust = _highest(
            (str(item.get("trustworthiness", "trusted")) for item in source_security),
            _TRUST_ORDER,
            "trusted",
        )
        confidentiality = _highest(
            (str(item.get("confidentiality", "public")) for item in source_security),
            _CONFIDENTIALITY_ORDER,
            "public",
        )
        risk = metadata.get("risk_level") or metadata.get("skill_risk_level")
        if tool.name == "execute_office_shell":
            risk = shell_risk_level(str(args.get("command", "")))
        risk = risk or _DEFAULT_RISK.get(capability, "high")
        if risk not in {"low", "medium", "high", "critical"}:
            risk = "high"
        authority = metadata.get("authority") or "model"
        if authority not in {"model", "contract", "human", "system"}:
            authority = "model"

        identity = f"{run_id or thread_id}:{tool_call_id}:{tool.name}"
        instruction_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        parent_instruction_id = None
        if source_security:
            parent_instruction_id = str(source_security[-1].get("instruction_id") or "") or None

        return InstructionEnvelope(
            instruction_id=instruction_id,
            run_id=run_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            parent_instruction_id=parent_instruction_id,
            tool_name=tool.name,
            args=args,
            capability=capability,
            resource=resource,
            reference_tool_ids=references,
            trustworthiness=trust,  # type: ignore[arg-type]
            confidentiality=confidentiality,  # type: ignore[arg-type]
            risk_level=risk,
            authority=authority,
            governance_mode=self.governance_mode,
        )
