from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


ContractStatus = Literal["draft", "approved", "archived"]
RiskLevel = Literal["low", "medium", "high", "critical"]
DecisionType = Literal["allow", "deny", "require_confirmation"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContractScope(ContractModel):
    can_read: list[str] = Field(default_factory=list)
    can_write: list[str] = Field(default_factory=list)
    cannot_write: list[str] = Field(default_factory=list)
    can_execute: list[str] = Field(default_factory=list)
    cannot_execute: list[str] = Field(default_factory=list)
    autonomous: list[str] = Field(default_factory=list)
    review_required: list[str] = Field(default_factory=list)
    human_only: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class ContractLimits(ContractModel):
    max_tool_calls: Optional[int] = Field(default=None, ge=0)
    max_shell_seconds: int = Field(default=60, ge=1, le=3600)
    max_written_files: Optional[int] = Field(default=None, ge=0)
    max_file_bytes: Optional[int] = Field(default=None, ge=0)


class ToolPolicy(ContractModel):
    allowed_tools: list[str] = Field(default_factory=list)
    blocked_tools: list[str] = Field(default_factory=list)
    high_risk_tools: list[str] = Field(default_factory=list)
    require_confirmation_for: list[str] = Field(default_factory=list)


class AcceptanceRule(ContractModel):
    type: str
    path: Optional[str] = None
    tool: Optional[str] = None


class ApprovalInfo(ContractModel):
    mode: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    contract_hash: Optional[str] = None


class RuntimePolicy(ContractModel):
    strict_contract_required: bool = False


class TaskContract(ContractModel):
    contract_version: str
    id: str
    owner: str
    objective: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    deliverables: list[str] = Field(default_factory=list)
    rollback_plan: Optional[str] = None
    escalation: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = "low"
    status: ContractStatus = "draft"
    scope: ContractScope
    tool_policy: ToolPolicy
    limits: ContractLimits = Field(default_factory=ContractLimits)
    acceptance: list[AcceptanceRule] = Field(default_factory=list)
    approval: Optional[ApprovalInfo] = None
    runtime_policy: RuntimePolicy = Field(default_factory=RuntimePolicy)


class ContractDecision(ContractModel):
    decision: DecisionType
    contract_id: Optional[str] = None
    clause: Optional[str] = None
    reason: str = ""
    risk_level: Optional[RiskLevel] = None
    contract_hash: Optional[str] = None
