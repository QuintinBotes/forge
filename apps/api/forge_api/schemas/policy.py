"""F29 — request/response models for the conditional policy routes.

Foundation deviation (see slice notes): the slice's routes are
``/policy/repos/{repo_connection_id}/...`` resolving a snapshot via integration-sdk.
This foundation's F04 router is path/inline-policy based (there is no
policy-fetch-by-connection), so these models carry an inline ``policy`` or a
``repo_root`` path instead of a connection id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from forge_contracts import Decision, Policy, RuleEffect, ToolCall
from forge_policy import PolicyContext
from forge_policy.tests_runner import PolicyTestReport, PolicyTestSuite


class SimulateRequest(BaseModel):
    """Body for ``POST /policy/simulate`` — a dry-run with full rule trace."""

    action: ToolCall
    context: PolicyContext = Field(default_factory=PolicyContext.empty)
    policy: Policy | None = None
    repo_root: str | None = None


class RuleTrace(BaseModel):
    """Per-rule outcome in a simulation (matched flag + why)."""

    rule_id: str
    matched: bool
    effect: RuleEffect
    reason: str


class SimulationResult(BaseModel):
    """Response for ``POST /policy/simulate``."""

    decision: Decision
    base_effect: Literal["allow", "deny", "requires_approval"]
    traces: list[RuleTrace] = Field(default_factory=list)


class PolicyTestRequest(BaseModel):
    """Body for ``POST /policy/test`` — run a policy-as-code assertion suite."""

    policy: Policy | None = None
    repo_root: str | None = None
    suite: PolicyTestSuite | None = None


class PolicyTestResponse(PolicyTestReport):
    """Response for ``POST /policy/test`` (the run report)."""


class PolicyRuleEvaluationOut(BaseModel):
    """An append-only ``policy_rule_evaluation`` audit row (read projection)."""

    id: str
    action: str
    base_effect: str
    final_effect: str
    requires_approval: bool
    severity: str
    matched_rule_ids: list[str] = Field(default_factory=list)
    context_redacted: dict[str, Any] = Field(default_factory=dict)
    agent_run_id: str | None = None
    step_id: str | None = None
    evaluated_at: datetime


__all__ = [
    "PolicyRuleEvaluationOut",
    "PolicyTestRequest",
    "PolicyTestResponse",
    "RuleTrace",
    "SimulateRequest",
    "SimulationResult",
]
