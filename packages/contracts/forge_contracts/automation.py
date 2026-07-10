"""Shared automation contracts (F21 — saved workflow automations).

This is the frozen substrate the board/workflow producers, the pure rule engine
(``forge_board.automation``), the API persistence layer, and the Celery worker
all build against — so producers depend on *contracts*, never on the engine.

It defines the persisted enum vocabulary, the trigger *envelope* that carries a
domain event into the engine, the :class:`AutomationDispatcher` Protocol (board /
workflow producers call this post-commit), and the ``HUMAN_GATE_EVENTS`` guard
set that an automation may never send (it can never approve a spec/plan/PR).

Foundation deviation note: the idealized F21 slice assumed an ``status_id``/
``task_statuses`` board model with label/team tables. The real foundation
(``forge_db.models.Task``) uses a ``status`` *enum* and has no label/team tables,
so the enum/field vocabulary below conforms to the real model (status enum;
no label triggers/actions). See the slice notes.

F40-AUT-ACTIONS adds the sprint lifecycle triggers (``SPRINT_STARTED`` /
``SPRINT_COMPLETED``) and the external/incident/merge action vocabulary
(``WEBHOOK_POST`` / ``CREATE_EXTERNAL_ISSUE`` / ``TRIGGER_DEPLOY`` /
``DECLARE_INCIDENT`` / ``START_SPRINT`` / ``AUTO_MERGE``) — see
``forge_board.automation.executor.ExternalActionExecutor``.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# The condition DSL is defined ONCE in ``forge_contracts.conditions`` (the shared,
# whitelisted primitive lifted in F29). F21 previously carried its own drifted
# ``ConditionOp`` copy; it now re-exports the shared enum so producers, the rule
# engine, the API, and the policy engine all speak one vocabulary.
from forge_contracts.conditions import ConditionOp


class AutomationTriggerType(enum.StrEnum):
    """Domain events an automation rule can react to."""

    TASK_CREATED = "task_created"
    TASK_STATUS_CHANGED = "task_status_changed"
    TASK_ASSIGNED = "task_assigned"
    TASK_PRIORITY_CHANGED = "task_priority_changed"
    TASK_SLA_BREACHED = "task_sla_breached"
    WORKFLOW_STATE_CHANGED = "workflow_state_changed"  # config: {to_state: "merged"}
    PR_MERGED = "pr_merged"
    APPROVAL_RESOLVED = "approval_resolved"
    #: Fired by Celery Beat on a cron cadence; config: {cron: "0 9 * * *"}.
    SCHEDULED = "scheduled"
    #: F40: a sprint transitioned to ``active`` / ``completed`` (``forge_board``
    #: ``SprintService``).
    SPRINT_STARTED = "sprint_started"
    SPRINT_COMPLETED = "sprint_completed"


class AutomationActionType(enum.StrEnum):
    """The fixed, board/workflow-internal action catalog (no scope expansion)."""

    SET_STATUS = "set_status"
    SET_PRIORITY = "set_priority"
    SET_ASSIGNEE = "set_assignee"
    SET_FIELD = "set_field"  # sprint_id | milestone_id | estimate
    ADD_COMMENT = "add_comment"
    CLOSE_LINKED_SPEC_TASKS = "close_linked_spec_tasks"
    SEND_WORKFLOW_EVENT = "send_workflow_event"  # non-human-gate events ONLY
    SEND_NOTIFICATION = "send_notification"
    CREATE_TASK = "create_task"
    # -- F40-AUT-ACTIONS: external + incident + sprint + merge actions -------- #
    WEBHOOK_POST = "webhook_post"
    CREATE_EXTERNAL_ISSUE = "create_external_issue"  # via a connected PM adapter
    TRIGGER_DEPLOY = "trigger_deploy"  # policy-gated via deploy_rules
    DECLARE_INCIDENT = "declare_incident"  # wired to the incident service
    START_SPRINT = "start_sprint"  # auto-start the next planned sprint
    AUTO_MERGE = "auto_merge"  # DEFAULT OFF; double-gated (opt-in + policy)


class AutomationExecutionStatus(enum.StrEnum):
    """The outcome of one rule evaluation against one source event."""

    SUCCEEDED = "succeeded"
    CONDITIONS_FAILED = "conditions_failed"
    NO_OP = "no_op"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"
    SKIPPED_LOOP = "skipped_loop"
    SKIPPED_DISABLED = "skipped_disabled"


class AutomationTriggerSource(enum.StrEnum):
    """Which append-only producer the source event came from."""

    BOARD_ACTIVITY = "board_activity"
    WORKFLOW_TRANSITION = "workflow_transition"
    #: A Celery Beat cron tick (the F40 scheduled-trigger producer).
    SCHEDULER = "scheduler"


class AutomationEntityType(enum.StrEnum):
    """The kind of entity a trigger fired for."""

    TASK = "task"
    EPIC = "epic"
    INCIDENT = "incident"
    SPRINT = "sprint"


#: Workflow events that grant a human approval — an automation may NEVER send
#: these (double-enforced at validate time + run time). Conforms to the real
#: ``default_feature`` workflow's ``*_by_human`` gate events.
HUMAN_GATE_EVENTS: frozenset[str] = frozenset(
    {
        "spec_approved_by_human",
        "plan_approved_by_human",
        "review_approved_by_human",
    }
)


class AutomationTriggerEnvelope(BaseModel):
    """A domain event carried into the engine for evaluation.

    Producers (board / workflow) construct this post-commit and hand it to an
    :class:`AutomationDispatcher`; ``depth``/``causation_chain`` carry the loop
    metadata (0 / [] for human/agent-originated events).
    """

    trigger_type: AutomationTriggerType
    trigger_source: AutomationTriggerSource
    trigger_event_id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID | None = None
    entity_type: AutomationEntityType = AutomationEntityType.TASK
    entity_id: uuid.UUID
    change: dict[str, Any] = Field(default_factory=dict)
    depth: int = 0
    causation_chain: list[uuid.UUID] = Field(default_factory=list)


@runtime_checkable
class AutomationDispatcher(Protocol):
    """Board/workflow producers depend on this — never on the engine."""

    def dispatch(self, envelope: AutomationTriggerEnvelope) -> None: ...


class NullAutomationDispatcher:
    """Test/seam double: records envelopes, never enqueues."""

    def __init__(self) -> None:
        self.dispatched: list[AutomationTriggerEnvelope] = []

    def dispatch(self, envelope: AutomationTriggerEnvelope) -> None:
        self.dispatched.append(envelope)


__all__ = [
    "HUMAN_GATE_EVENTS",
    "AutomationActionType",
    "AutomationDispatcher",
    "AutomationEntityType",
    "AutomationExecutionStatus",
    "AutomationTriggerEnvelope",
    "AutomationTriggerSource",
    "AutomationTriggerType",
    "ConditionOp",
    "NullAutomationDispatcher",
]
