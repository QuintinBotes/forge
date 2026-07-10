"""Pydantic v2 schemas for the automation rule engine (F21).

These are the YAML/JSON-portable rule body (``AutomationRuleSpec``) plus the
in-memory evaluation inputs (``EntitySnapshot``) and outputs (``ActionResult`` /
``ExecutionResult``).

Foundation deviation: ``SetStatusAction`` carries a ``status`` *enum*
(``TaskStatus``) rather than the slice's ``status_id`` / ``status_category``
because the real ``Task`` model stores status as an enum with no per-project
status table. ``CloseLinkedSpecTasksAction`` likewise targets a ``TaskStatus``.
Label actions/triggers are omitted (no label table in the foundation).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.automation import (
    AutomationActionType,
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerType,
)

# Conditions are the shared, whitelisted primitive (``forge_contracts.conditions``).
# F21 previously carried its own ``Condition``/``ConditionGroup`` copy; they are
# re-exported here so the engine, the API, and the policy engine share one model.
from forge_contracts.conditions import Condition, ConditionGroup
from forge_contracts.enums import IncidentSeverity, Priority, TaskKind, TaskStatus

# --------------------------------------------------------------------------- #
# Conditions (evaluation inputs)                                               #
# --------------------------------------------------------------------------- #


class EntitySnapshot(BaseModel):
    """The current entity fields + trigger-local change context."""

    entity_type: AutomationEntityType
    entity_id: uuid.UUID
    fields: dict[str, Any] = Field(default_factory=dict)
    change: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Actions (discriminated on ``type``)                                          #
# --------------------------------------------------------------------------- #


class _ActionBase(BaseModel):
    model_config = ConfigDict(use_enum_values=False)


class SetStatusAction(_ActionBase):
    type: Literal[AutomationActionType.SET_STATUS] = AutomationActionType.SET_STATUS
    status: TaskStatus


class SetPriorityAction(_ActionBase):
    type: Literal[AutomationActionType.SET_PRIORITY] = AutomationActionType.SET_PRIORITY
    priority: Priority


class SetAssigneeAction(_ActionBase):
    type: Literal[AutomationActionType.SET_ASSIGNEE] = AutomationActionType.SET_ASSIGNEE
    assignee_id: uuid.UUID | None = None  # None = unassign


class SetFieldAction(_ActionBase):
    type: Literal[AutomationActionType.SET_FIELD] = AutomationActionType.SET_FIELD
    field: Literal["sprint_id", "milestone_id", "estimate"]
    value: Any = None


class AddCommentAction(_ActionBase):
    type: Literal[AutomationActionType.ADD_COMMENT] = AutomationActionType.ADD_COMMENT
    body_template: str = Field(max_length=4000)


class CloseLinkedSpecTasksAction(_ActionBase):
    type: Literal[AutomationActionType.CLOSE_LINKED_SPEC_TASKS] = (
        AutomationActionType.CLOSE_LINKED_SPEC_TASKS
    )
    scope: Literal["project", "workspace"] = "project"
    target_status: TaskStatus = TaskStatus.DONE
    exclude_trigger_task: bool = True


class SendWorkflowEventAction(_ActionBase):
    type: Literal[AutomationActionType.SEND_WORKFLOW_EVENT] = (
        AutomationActionType.SEND_WORKFLOW_EVENT
    )
    event: str  # validator REJECTS HUMAN_GATE_EVENTS at save time


class SendNotificationAction(_ActionBase):
    type: Literal[AutomationActionType.SEND_NOTIFICATION] = AutomationActionType.SEND_NOTIFICATION
    channel: Literal["slack", "email"]
    target: str
    message_template: str = Field(max_length=4000)


class CreateTaskAction(_ActionBase):
    type: Literal[AutomationActionType.CREATE_TASK] = AutomationActionType.CREATE_TASK
    title_template: str = Field(max_length=512)
    kind: TaskKind = TaskKind.CHORE


# --------------------------------------------------------------------------- #
# F40-AUT-ACTIONS: external + incident + sprint + merge actions                #
# --------------------------------------------------------------------------- #
#
# These are dispatched by ``forge_board.automation.executor.ExternalActionExecutor``
# rather than the worker's DB executor: each performs (or requests) a side effect
# outside the board DB — a webhook, a PM-adapter issue, a deploy, an incident
# declaration, a sprint start, or a merge — and is policy-gated + audited there.


class WebhookPostAction(_ActionBase):
    """POST a JSON payload to an arbitrary URL (e.g. a CI/chat webhook)."""

    type: Literal[AutomationActionType.WEBHOOK_POST] = AutomationActionType.WEBHOOK_POST
    url: str = Field(max_length=2000)
    payload_template: dict[str, Any] = Field(default_factory=dict)


class CreateExternalIssueAction(_ActionBase):
    """Create an issue in a connected external PM system (F40-PM-ADAPTERS)."""

    type: Literal[AutomationActionType.CREATE_EXTERNAL_ISSUE] = (
        AutomationActionType.CREATE_EXTERNAL_ISSUE
    )
    provider: str = Field(max_length=32)  # forge_contracts.pm.PMProvider value
    title_template: str = Field(default="{{rule.name}}", max_length=512)


class TriggerDeployAction(_ActionBase):
    """Trigger a deploy (outbound side mocked until a real CD webhook lands).

    Gated by ``deploy_rules`` (``forge_policy``): denied unless the target
    environment is explicitly whitelisted and agent deploys are enabled.
    """

    type: Literal[AutomationActionType.TRIGGER_DEPLOY] = AutomationActionType.TRIGGER_DEPLOY
    environment: str = Field(max_length=32)
    ref_template: str = Field(default="{{entity.id}}", max_length=256)


class DeclareIncidentAction(_ActionBase):
    """Declare an incident (wired to the incident service) — e.g. on SLA breach."""

    type: Literal[AutomationActionType.DECLARE_INCIDENT] = AutomationActionType.DECLARE_INCIDENT
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    title_template: str = Field(default="SLA breach: {{entity.id}}", max_length=200)


class StartSprintAction(_ActionBase):
    """Auto-start the next planned sprint in the triggering project."""

    type: Literal[AutomationActionType.START_SPRINT] = AutomationActionType.START_SPRINT


class AutoMergeAction(_ActionBase):
    """Merge a pull request automatically.

    DEFAULT OFF: ``enabled`` must be explicitly set ``True`` by the rule author,
    *and* the runtime executor re-checks the repo policy's
    ``review_rules.approval_required_for_merge`` (double-enforced, mirroring the
    ``HUMAN_GATE_EVENTS`` guard) — an automation can never merge on a repo whose
    policy still requires human approval, regardless of this flag.
    """

    type: Literal[AutomationActionType.AUTO_MERGE] = AutomationActionType.AUTO_MERGE
    enabled: bool = False
    merge_method: Literal["merge", "squash", "rebase"] = "squash"


ActionSpec = Annotated[
    SetStatusAction
    | SetPriorityAction
    | SetAssigneeAction
    | SetFieldAction
    | AddCommentAction
    | CloseLinkedSpecTasksAction
    | SendWorkflowEventAction
    | SendNotificationAction
    | CreateTaskAction
    | WebhookPostAction
    | CreateExternalIssueAction
    | TriggerDeployAction
    | DeclareIncidentAction
    | StartSprintAction
    | AutoMergeAction,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# Trigger + rule                                                               #
# --------------------------------------------------------------------------- #


class TriggerSpec(BaseModel):
    type: AutomationTriggerType
    config: dict[str, Any] = Field(default_factory=dict)


class AutomationRuleSpec(BaseModel):
    """The YAML/JSON-portable rule body."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    enabled: bool = True
    trigger: TriggerSpec
    condition: ConditionGroup = Field(default_factory=ConditionGroup)
    actions: list[ActionSpec] = Field(min_length=1)
    run_order: int = 100


class AutomationRuleSpecWithMeta(BaseModel):
    """A rule spec plus the persisted metadata the engine needs to evaluate."""

    spec: AutomationRuleSpec
    id: uuid.UUID
    version: int = 1
    enabled: bool = True
    run_order: int = 100


# --------------------------------------------------------------------------- #
# Results                                                                      #
# --------------------------------------------------------------------------- #


class ActionResult(BaseModel):
    type: AutomationActionType
    status: Literal["ok", "no_op", "error", "forbidden"]
    detail: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ExecutionResult:
    """One evaluated rule's outcome (the caller persists these)."""

    rule_id: uuid.UUID
    rule_version: int
    status: AutomationExecutionStatus
    condition_result: bool | None
    actions_planned: list[Any]
    action_results: list[ActionResult]
    depth: int
    causation_chain: list[uuid.UUID] = field(default_factory=list)
    error: str | None = None


__all__ = [
    "ActionResult",
    "ActionSpec",
    "AddCommentAction",
    "AutoMergeAction",
    "AutomationRuleSpec",
    "AutomationRuleSpecWithMeta",
    "CloseLinkedSpecTasksAction",
    "Condition",
    "ConditionGroup",
    "CreateExternalIssueAction",
    "CreateTaskAction",
    "DeclareIncidentAction",
    "EntitySnapshot",
    "ExecutionResult",
    "SendNotificationAction",
    "SendWorkflowEventAction",
    "SetAssigneeAction",
    "SetFieldAction",
    "SetPriorityAction",
    "SetStatusAction",
    "StartSprintAction",
    "TriggerDeployAction",
    "TriggerSpec",
    "WebhookPostAction",
]
