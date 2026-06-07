from typing import Literal, Optional
from pydantic import BaseModel, Field


ContractStatus = Literal["draft", "approved", "archived"]
RiskLevel = Literal["low", "medium", "high", "critical"]
DecisionType = Literal["allow", "deny", "require_confirmation"]


class ContractScope(BaseModel):
    can_read: list[str] = Field(default_factory=list)
    can_write: list[str] = Field(default_factory=list)
    cannot_write: list[str] = Field(default_factory=list)
    can_execute: list[str] = Field(default_factory=list)
    cannot_execute: list[str] = Field(default_factory=list)


class ContractLimits(BaseModel):
    max_tool_calls: Optional[int] = None
    max_shell_seconds: int = 60
    max_written_files: Optional[int] = None
    max_file_bytes: Optional[int] = None


class ToolPolicy(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    blocked_tools: list[str] = Field(default_factory=list)
    high_risk_tools: list[str] = Field(default_factory=list)
    require_confirmation_for: list[str] = Field(default_factory=list)


class AcceptanceRule(BaseModel):
    type: str
    path: Optional[str] = None
    tool: Optional[str] = None


class ApprovalInfo(BaseModel):
    mode: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    contract_hash: Optional[str] = None


class RuntimePolicy(BaseModel):
    strict_contract_required: bool = False


class TaskContract(BaseModel):
    contract_version: str
    id: str
    owner: str
    objective: str
    risk_level: RiskLevel = "low"
    status: ContractStatus = "draft"
    scope: ContractScope
    tool_policy: ToolPolicy
    limits: ContractLimits = Field(default_factory=ContractLimits)
    acceptance: list[AcceptanceRule] = Field(default_factory=list)
    approval: Optional[ApprovalInfo] = None
    runtime_policy: RuntimePolicy = Field(default_factory=RuntimePolicy)


class ContractDecision(BaseModel):
    decision: DecisionType
    contract_id: Optional[str] = None
    clause: Optional[str] = None
    reason: str = ""
    risk_level: Optional[RiskLevel] = None
    contract_hash: Optional[str] = None
