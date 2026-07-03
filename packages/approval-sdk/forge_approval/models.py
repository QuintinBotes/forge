"""Domain models for the Human Approval System (F36).

Conforms to the foundation: the six gate types reuse the frozen
:class:`forge_contracts.enums.ApprovalGate` verbatim (aliased ``GateType`` — no
parallel enum), and RBAC roles reuse the frozen
:class:`forge_contracts.enums.UserRole` (aliased ``Role``).

Deviation note (vs. the F36 slice doc): the doc names the column/enum
``gate_type`` with a fresh ``GateType`` enum; the real foundation already ships
``ApprovalGate`` with exactly the six spec values on the ``approval_request.gate``
column, so this package aliases rather than duplicates. ``GateStatus`` is the
domain superset of the frozen ``ApprovalStatus`` — it adds the ``expired``
terminal state written by the SLA sweeper (the frozen contract enum cannot be
widened; the DB-side enum gains the value additively).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.enums import ApprovalGate, UserRole

#: The six approval gate types (frozen foundation enum, aliased).
GateType = ApprovalGate

#: RBAC roles (frozen foundation enum, aliased).
Role = UserRole

#: Risk levels in ascending severity order (drives inbox sort + escalation).
RISK_LEVELS: tuple[str, ...] = ("info", "warning", "critical")

RiskLevel = Literal["info", "warning", "critical"]


class GateStatus(enum.StrEnum):
    """Lifecycle of an approval gate (``ApprovalStatus`` + ``expired``)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    EXPIRED = "expired"


class ApprovalAction(enum.StrEnum):
    """The decisions a reviewer can record on a gate."""

    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    ESCALATE = "escalate"


class Principal(BaseModel):
    """The actor attempting to act on a gate (user, agent, or system)."""

    kind: Literal["user", "agent", "system"]
    id: UUID | None = None
    role: Role | None = None
    workspace_id: UUID

    @property
    def actor_ref(self) -> str:
        """Stable ``kind:<id>`` reference (matches ``requested_actor``)."""
        return "system" if self.kind == "system" else f"{self.kind}:{self.id}"


class RiskFlag(BaseModel):
    """One entry in the "Risks flagged" panel (must-show item 7)."""

    severity: RiskLevel = "info"
    category: str = "policy"
    message: str
    source: str | None = None


class ApprovalContext(BaseModel):
    """The spec's nine "must-show" items; ``None``/empty sections are hidden."""

    approval_id: UUID
    gate_type: GateType
    goal: str = ""  # 1 — goal & requirements
    requirements: list[dict[str, Any]] = Field(default_factory=list)  # 1
    diff: dict[str, Any] | None = None  # 2 — changed files (pr/deploy)
    verification: dict[str, Any] | None = None  # 3 — lint/type/test/coverage (pr)
    traceability: list[dict[str, Any]] | None = None  # 4 — spec traceability (pr)
    knowledge_refs: list[dict[str, Any]] | None = None  # 5 — provenance
    confidence: dict[str, Any] | None = None  # 6 — {score, rationale}
    risk_flags: list[RiskFlag] = Field(default_factory=list)  # 7 — always shown
    run_trace_ref: dict[str, Any] | None = None  # 8 — {workflow_run_id, agent_run_id}
    available_actions: list[ApprovalAction] = Field(default_factory=list)  # 9
    gate_payload: dict[str, Any] = Field(default_factory=dict)  # gate-specific extras


class ApprovalRequest(BaseModel):
    """The domain view of one ``approval_request`` row."""

    id: UUID
    workspace_id: UUID
    project_id: UUID | None = None
    gate_type: GateType
    status: GateStatus = GateStatus.PENDING
    subject_type: str = "workflow_run"
    subject_id: UUID | None = None
    workflow_run_id: UUID | None = None
    agent_run_id: UUID | None = None
    task_id: UUID | None = None
    required_approvals: int = 1
    risk_level: RiskLevel = "info"
    title: str | None = None
    gate_payload: dict[str, Any] = Field(default_factory=dict)
    context_ref: str | None = None
    requested_by: UUID | None = None
    requested_actor: str = "system"
    #: Escalation raises the resolving bar to admin; recorded on the request so
    #: the authorizer enforces it on every subsequent resolve attempt.
    escalated: bool = False
    decision_note: str | None = None
    resolver_user_id: UUID | None = None
    expires_at: datetime | None = None
    requested_at: datetime | None = None
    resolved_at: datetime | None = None


class ApprovalSummary(BaseModel):
    """One approval-inbox row."""

    id: UUID
    gate_type: GateType
    status: GateStatus
    title: str
    project_id: UUID | None = None
    risk_level: str = "info"
    requested_actor: str = "system"
    requested_at: datetime | None = None


class ApprovalDecisionRequest(BaseModel):
    """Body of a resolve call — one reviewer's decision."""

    decision: ApprovalAction
    note: str | None = None


class ApprovalDecisionRecord(BaseModel):
    """One immutable per-approver decision row."""

    approval_request_id: UUID
    approver_user_id: UUID
    decision: ApprovalAction
    note: str | None = None
    created_at: datetime | None = None


class ResolutionOutcome(BaseModel):
    """What the gate's resolution hook did (or could not yet do)."""

    completed: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    follow_up_state: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ApprovalResolution(BaseModel):
    """Result of ``ApprovalService.resolve`` — gate status + hook outcome."""

    approval_id: UUID
    status: GateStatus
    outcome: ResolutionOutcome


class PolicyOverrideGrant(BaseModel):
    """A single-use, short-TTL permission for one exact tool call (J5).

    Bound to an ``action_fingerprint``; consuming it never broadens future
    scope (Build-Prompt constraint #2).
    """

    model_config = ConfigDict(frozen=False)

    id: UUID
    approval_request_id: UUID
    agent_run_id: UUID
    action_fingerprint: str
    granted_by: UUID
    consumed: bool = False
    expires_at: datetime
    created_at: datetime | None = None


def risk_rank(level: str) -> int:
    """Ascending severity rank of a risk level (unknown levels rank lowest)."""
    try:
        return RISK_LEVELS.index(level)
    except ValueError:
        return -1


def max_risk(a: str, b: str) -> str:
    """The more severe of two risk levels."""
    return a if risk_rank(a) >= risk_rank(b) else b


__all__ = [
    "RISK_LEVELS",
    "ApprovalAction",
    "ApprovalContext",
    "ApprovalDecisionRecord",
    "ApprovalDecisionRequest",
    "ApprovalRequest",
    "ApprovalResolution",
    "ApprovalSummary",
    "GateStatus",
    "GateType",
    "PolicyOverrideGrant",
    "Principal",
    "ResolutionOutcome",
    "RiskFlag",
    "RiskLevel",
    "Role",
    "max_risk",
    "risk_rank",
]
