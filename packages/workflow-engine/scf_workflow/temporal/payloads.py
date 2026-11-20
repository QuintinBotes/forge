"""Typed payloads for the Temporal feature workflow + activities (F25).

Pure dataclasses (no IO) so they import cleanly inside the workflow determinism
sandbox *and* in the activity worker. Serialized by the pydantic data converter
(handles ``UUID`` / ``WorkflowState`` StrEnum / ``datetime``) and then wrapped by
the :class:`RedactingEncryptionCodec` before they ever reach Temporal history.

Events are plain DSL tokens (``str``) — the foundation's ``WorkflowEngine.transition``
takes ``event: str`` and the ``default_feature`` DSL keys transitions on those
tokens, so F25 conforms rather than inventing a ``WorkflowEventType`` enum.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from scf_contracts import WorkflowState

# The Red-Team gate payloads + verdict constants moved to the engine-agnostic
# ``scf_workflow.red_team_gate`` (Task 20 — V1 gate parity); re-exported here
# unchanged so every Temporal import path keeps working byte-identically.
from scf_workflow.red_team_gate import (
    REDTEAM_BLOCKED,
    REDTEAM_NOT_RUN,
    REDTEAM_SURVIVED,
    RedTeamInput,
    RedTeamResult,
)

# -- canonical human/agent gate event tokens (subset of the DSL vocabulary) -- #
EVENT_SPEC_APPROVED = "spec_approved_by_human"
EVENT_SPEC_CHANGES = "spec_changes_requested"
EVENT_PLAN_APPROVED = "plan_approved_by_human"
EVENT_REVIEW_APPROVED = "review_approved_by_human"
EVENT_RESUME = "resume"
EVENT_CANCEL = "cancel"


@dataclass
class RetryPolicyDTO:
    max_retries: int = 3
    backoff: str = "exponential"
    initial_delay_seconds: int = 30


@dataclass
class EscalationPolicyDTO:
    confidence_threshold: float = 0.72
    on_low_confidence: str = "pause_and_notify"
    on_policy_conflict: str = "escalate_to_admin"


@dataclass
class WorkflowParams:
    """Start argument for ``FeatureWorkflow.run``."""

    workflow_run_id: uuid.UUID
    task_id: uuid.UUID
    workspace_id: uuid.UUID
    definition_name: str = "default_feature"
    definition_version: str = "1"
    execution_mode: str = "single_agent"
    retry_policy: RetryPolicyDTO = field(default_factory=RetryPolicyDTO)
    escalation_policy: EscalationPolicyDTO = field(default_factory=EscalationPolicyDTO)
    # Who started the run (an issue's label sender, say); the run's first transition
    # names them. Defaulted, so histories recorded before it replay unchanged.
    started_by: str = "system"
    # The check activity's time limit (#172); defaulted so recorded histories replay.
    checks_timeout_seconds: int = 1_800


@dataclass
class StationParams:
    """Start argument for ``StationWorkflow.run`` (spec K §4.2).

    The revision's text rides in the params, so a run never reads its definition
    from the database and a later publish never reaches it.
    """

    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    definition_revision_id: uuid.UUID
    dsl_yaml: str
    #: The task's ``key``, ``title``, ``description`` and (one target only) ``repo``.
    task: dict[str, str] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceStationInput:
    """Argument to ``forge.run_service_station``: one service station visit."""

    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    idempotency_key: str
    params: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceStationResult:
    outcome: str = "succeeded"
    outputs: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class StationGateInput:
    """Argument to ``forge.open_station_gate`` and ``forge.read_station_gate`` (spec K §4.2).

    It names the visit; the activities find its gate by the visit, never by an id
    a Signal carried.
    """

    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    state: str
    visit: int
    gate: str  # spec | plan | pr
    expires_at: datetime
    required_approvals: int = 1
    summary: str = ""
    #: ``read_station_gate`` only: the deadline passed, so a gate still pending expires.
    expire: bool = False


@dataclass
class StationGateResult:
    approval_request_id: uuid.UUID
    status: str  # pending | approved | rejected | changes_requested | expired
    #: ``user:<id>`` of whoever decided a decided gate.
    actor: str | None = None


@dataclass
class WorkflowEventPayload:
    """An Update/Signal argument carrying a human/agent gate decision."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"
    confidence: float | None = None
    idempotency_key: str | None = None


@dataclass
class WorkflowResult:
    final_state: WorkflowState
    transition_count: int
    failure_reason: str | None = None


@dataclass
class TransitionRecord:
    """One append-only transition the ``persist_transition`` activity records."""

    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    # Open strings (spec K §4.3): feature runs pass ``WorkflowState`` members,
    # which serialize to the same JSON strings, so older payloads still load.
    from_state: str
    to_state: str
    event: str
    idempotency_key: str
    guard_results: dict[str, bool] = field(default_factory=dict)
    effects_dispatched: list[str] = field(default_factory=list)
    record: str | None = None
    actor: str = "system"
    skill: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    temporal_run_id: str | None = None
    #: Station runs only (spec K §4.2): the run status their terminal sets.
    run_status: str | None = None


@dataclass
class StationVisitRecord:
    """One station visit the ``persist_station`` activity upserts (spec K §4.3).

    The visit's id is ``station_visit_id(workflow_run_id, state, visit)``. The field
    names are the ``workflow_station_visit`` columns'.
    """

    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    definition_revision_id: uuid.UUID
    state: str
    visit: int
    kind: str  # agent | service | human
    status: str  # running | waiting | succeeded | failed | parked | cancelled
    outcome: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    service_name: str | None = None
    agent_run_id: uuid.UUID | None = None
    approval_request_id: uuid.UUID | None = None
    tokens_used: int = 0
    #: ``None`` while running or when the model is unpriced.
    cost_usd: float | None = None
    priced: bool = False


@dataclass
class GuardInputsRequest:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    phase: str  # "plan" | "execute" | "merge"


@dataclass
class GuardInputs:
    """IO-resolved guard data the workflow then evaluates deterministically."""

    plan_required: bool = True
    preconditions: dict[str, bool] = field(default_factory=dict)
    ci_status_green: bool | None = None
    spec_validated: bool | None = None


@dataclass
class RunAgentInput:
    workflow_run_id: uuid.UUID
    task_id: uuid.UUID
    workspace_id: uuid.UUID
    attempt: int
    idempotency_key: str
    # Station runs only (spec K §4.2); feature runs leave them unset.
    station_visit_id: uuid.UUID | None = None
    role: str | None = None
    skill_profile: str | None = None
    objective: str | None = None
    context_template: str | None = None
    model: str | None = None
    max_tokens: int | None = None


@dataclass
class ResumeAgentInput:
    workflow_run_id: uuid.UUID
    task_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_run_id: uuid.UUID | None
    attempt: int
    idempotency_key: str
    #: The person's input when a parked station resumes (spec K §4.2).
    input: str | None = None


@dataclass
class AgentRunResultDTO:
    """Projection of the F06 agent result relevant to the FSM spine."""

    agent_run_id: uuid.UUID
    status: str  # succeeded | failed | awaiting_input | cancelled
    confidence: float = 1.0
    needs_human_reason: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    branch_name: str | None = None
    head_commit_sha: str | None = None
    # Station runs (spec K §4.2): spend, and the agent's closed-set decision.
    tokens_used: int = 0
    cost_usd: float | None = None
    priced: bool = False
    decision: str | None = None


@dataclass
class RunChecksInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    attempt: int
    idempotency_key: str


@dataclass
class ChecksResult:
    results: dict[str, bool] = field(default_factory=dict)
    #: Set when the change cannot be judged without a person, such as
    #: ``reviewer_not_configured`` (#173). Absent from older histories.
    pause_reason: str | None = None

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(self.results.values())


@dataclass
class OpenPrInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    branch_name: str | None
    idempotency_key: str


@dataclass
class OpenPrResult:
    pr_number: int | None = None
    pr_url: str | None = None


@dataclass
class ApprovalInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    gate: str  # spec | plan | pr
    summary: str | None = None
    idempotency_key: str = ""


# (RedTeamInput / RedTeamResult / REDTEAM_* are re-exported from the
# engine-agnostic ``scf_workflow.red_team_gate`` — see the import block.)


@dataclass
class NotifyInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    reason: str
    kind: str = "pause_and_notify"  # pause_and_notify | escalate_to_admin
    idempotency_key: str = ""


@dataclass
class DraftScenariosInput:
    """Argument to the held-out scenario drafting step before the spec gate (#258)."""

    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    idempotency_key: str = ""


@dataclass
class DraftScenariosResult:
    """The drafted scenarios' ids; ``reason`` names why none were drafted.

    Never the scenarios' content: only their ids reach the workflow history.
    """

    scenario_ids: list[str] = field(default_factory=list)
    reason: str | None = None


@dataclass
class CleanupInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    reason: str = "cancelled"
    idempotency_key: str = ""
    # Who cancelled the run; the run's still-open gate requests expire as them.
    actor: str = "system"


__all__ = [
    "EVENT_CANCEL",
    "EVENT_PLAN_APPROVED",
    "EVENT_RESUME",
    "EVENT_REVIEW_APPROVED",
    "EVENT_SPEC_APPROVED",
    "EVENT_SPEC_CHANGES",
    "REDTEAM_BLOCKED",
    "REDTEAM_NOT_RUN",
    "REDTEAM_SURVIVED",
    "AgentRunResultDTO",
    "ApprovalInput",
    "ChecksResult",
    "CleanupInput",
    "DraftScenariosInput",
    "DraftScenariosResult",
    "EscalationPolicyDTO",
    "GuardInputs",
    "GuardInputsRequest",
    "NotifyInput",
    "OpenPrInput",
    "OpenPrResult",
    "RedTeamInput",
    "RedTeamResult",
    "ResumeAgentInput",
    "RetryPolicyDTO",
    "RunAgentInput",
    "RunChecksInput",
    "ServiceStationInput",
    "ServiceStationResult",
    "StationGateInput",
    "StationGateResult",
    "StationParams",
    "StationVisitRecord",
    "TransitionRecord",
    "WorkflowEventPayload",
    "WorkflowParams",
    "WorkflowResult",
]
