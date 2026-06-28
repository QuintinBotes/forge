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
from typing import Any

from forge_contracts import WorkflowState

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
    from_state: WorkflowState
    to_state: WorkflowState
    event: str
    idempotency_key: str
    guard_results: dict[str, bool] = field(default_factory=dict)
    effects_dispatched: list[str] = field(default_factory=list)
    record: str | None = None
    actor: str = "system"
    skill: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    temporal_run_id: str | None = None


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


@dataclass
class ResumeAgentInput:
    workflow_run_id: uuid.UUID
    task_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_run_id: uuid.UUID | None
    attempt: int
    idempotency_key: str


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


@dataclass
class RunChecksInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    attempt: int
    idempotency_key: str


@dataclass
class ChecksResult:
    results: dict[str, bool] = field(default_factory=dict)

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


@dataclass
class NotifyInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    reason: str
    kind: str = "pause_and_notify"  # pause_and_notify | escalate_to_admin
    idempotency_key: str = ""


@dataclass
class CleanupInput:
    workflow_run_id: uuid.UUID
    workspace_id: uuid.UUID
    reason: str = "cancelled"
    idempotency_key: str = ""


__all__ = [
    "EVENT_CANCEL",
    "EVENT_PLAN_APPROVED",
    "EVENT_RESUME",
    "EVENT_REVIEW_APPROVED",
    "EVENT_SPEC_APPROVED",
    "EVENT_SPEC_CHANGES",
    "AgentRunResultDTO",
    "ApprovalInput",
    "ChecksResult",
    "CleanupInput",
    "EscalationPolicyDTO",
    "GuardInputs",
    "GuardInputsRequest",
    "NotifyInput",
    "OpenPrInput",
    "OpenPrResult",
    "ResumeAgentInput",
    "RetryPolicyDTO",
    "RunAgentInput",
    "RunChecksInput",
    "TransitionRecord",
    "WorkflowEventPayload",
    "WorkflowParams",
    "WorkflowResult",
]
