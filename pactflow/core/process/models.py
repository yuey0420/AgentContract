"""Collaboration schema is versioned independently of legacy TaskContract."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class CollaborationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["collaboration/1"] = "collaboration/1"


class ContextEntry(CollaborationModel):
    text: str
    status: Literal["confirmed", "inferred", "hypothesis"] = "hypothesis"
    sources: list[str] = Field(default_factory=list)
    confirmed_by: str | None = None
    scope: str = "project"
    supersedes: str | None = None


class ProductContext(CollaborationModel):
    project_id: str
    entries: list[ContextEntry] = Field(default_factory=list)


class ScenarioAC(CollaborationModel):
    id: str
    description: str
    check_id: str
    required: bool = True
    kind: Literal["technical", "scenario"] = "scenario"


class WorkItem(CollaborationModel):
    work_item_id: str
    project_id: str
    objective: str
    owner: str
    non_goals: list[str] = Field(default_factory=list)
    scenarios: list[ScenarioAC] = Field(default_factory=list)
    acceptance_required: bool = True
    workspace_root: str
    status: Literal["active", "awaiting_acceptance", "needs_review", "archived"] = "active"


class PlanRevision(CollaborationModel):
    work_item_id: str
    steps: list[dict]
    predicted_files: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    basis: list[str] = Field(default_factory=list)


class DecisionRequest(CollaborationModel):
    kind: Literal["product_choice", "authorization_change", "exact_action", "acceptance", "budget", "clarification"]
    question: str
    options: list[dict]
    recommended_option: str | None = None
    blocked_node_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    authorization_effect: Literal["none"] = "none"


class Delivery(CollaborationModel):
    technical_status: Literal["passed", "failed", "limited", "not_run"]
    scenario_status: Literal["passed", "failed", "limited", "not_run"]
    human_acceptance: Literal["not_requested", "pending", "accepted", "rejected"]
    acceptance_required: bool
    limitations: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
