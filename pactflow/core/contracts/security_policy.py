import os
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .instructions import GovernanceMode, InstructionEnvelope


PolicyDecision = Literal["allow", "deny", "require_confirmation"]


class PolicyFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: str
    decision: PolicyDecision
    reason: str


class PolicyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecision
    effective_decision: PolicyDecision
    mode: GovernanceMode
    findings: list[PolicyFinding] = Field(default_factory=list)

    @property
    def primary_finding(self) -> PolicyFinding | None:
        if not self.findings:
            return None
        priority = {"allow": 0, "require_confirmation": 1, "deny": 2}
        return max(self.findings, key=lambda finding: priority[finding.decision])


class SecurityPolicy(Protocol):
    name: str

    def check(self, instruction: InstructionEnvelope) -> PolicyFinding | None:
        ...


class ConfidentialExternalFlowPolicy:
    name = "security.confidential_external_flow"

    def check(self, instruction: InstructionEnvelope) -> PolicyFinding | None:
        if (
            instruction.reference_tool_ids
            and instruction.capability == "external"
            and instruction.confidentiality in {"confidential", "restricted"}
        ):
            return PolicyFinding(
                policy=self.name,
                decision="deny",
                reason="Confidential tool output cannot flow into an external action.",
            )
        return None


class UntrustedSideEffectPolicy:
    name = "security.untrusted_side_effect"

    def check(self, instruction: InstructionEnvelope) -> PolicyFinding | None:
        if (
            instruction.reference_tool_ids
            and instruction.capability in {"write", "execute", "external"}
            and (
                instruction.trustworthiness == "untrusted"
                or (
                    instruction.trustworthiness == "unknown"
                    and instruction.capability == "external"
                )
            )
        ):
            return PolicyFinding(
                policy=self.name,
                decision="require_confirmation",
                reason="A side effect is derived from unknown or untrusted tool output.",
            )
        return None


class CriticalActionPolicy:
    name = "security.critical_action"

    def check(self, instruction: InstructionEnvelope) -> PolicyFinding | None:
        if instruction.risk_level == "critical":
            return PolicyFinding(
                policy=self.name,
                decision="require_confirmation",
                reason="Critical-risk actions require explicit human confirmation.",
            )
        return None


class SecurityPolicyRuntime:
    def __init__(
        self,
        policies: list[SecurityPolicy] | None = None,
        mode: GovernanceMode = "enforce",
    ):
        self.policies = policies or [
            ConfidentialExternalFlowPolicy(),
            UntrustedSideEffectPolicy(),
            CriticalActionPolicy(),
        ]
        self.mode = mode

    @classmethod
    def from_environment(cls) -> "SecurityPolicyRuntime":
        configured = os.getenv("PACTFLOW_GOVERNANCE_MODE", "enforce").strip().lower()
        mode: GovernanceMode = "observe" if configured == "observe" else "enforce"
        return cls(mode=mode)

    def check(self, instruction: InstructionEnvelope) -> PolicyEvaluation:
        findings = [
            finding
            for policy in self.policies
            if (finding := policy.check(instruction)) is not None
        ]
        priority = {"allow": 0, "require_confirmation": 1, "deny": 2}
        decision: PolicyDecision = "allow"
        if findings:
            decision = max(findings, key=lambda finding: priority[finding.decision]).decision
        effective: PolicyDecision = "allow" if self.mode == "observe" else decision
        return PolicyEvaluation(
            decision=decision,
            effective_decision=effective,
            mode=self.mode,
            findings=findings,
        )
