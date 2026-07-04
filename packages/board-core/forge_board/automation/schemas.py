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
    ConditionOp,
)
from forge_contracts.enums import Priority, TaskKind, TaskStatus

# --------------------------------------------------------------------------- #
# Conditions                                                                   #
# --------------------------------------------------------------------------- #


class Condition(BaseModel):
    """A single predicate: ``field op value`` evaluated against a snapshot."""

    field: str
    op: ConditionOp
    value: Any = None


class ConditionGroup(BaseModel):
    """A boolean tree of conditions. An empty group is always ``True``."""

    match: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(default_factory=list)
    groups: list[ConditionGroup] = Field(default_factory=list)


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
    type: Literal[AutomationActionType.SEND_NOTIFICATION] = (
        AutomationActionType.SEND_NOTIFICATION
    )
    channel: Literal["slack", "email"]
    target: str
    message_template: str = Field(max_length=4000)


class CreateTaskAction(_ActionBase):
    type: Literal[AutomationActionType.CREATE_TASK] = AutomationActionType.CREATE_TASK
    title_template: str = Field(max_length=512)
    kind: TaskKind = TaskKind.CHORE


ActionSpec = Annotated[
    SetStatusAction
    | SetPriorityAction
    | SetAssigneeAction
    | SetFieldAction
    | AddCommentAction
    | CloseLinkedSpecTasksAction
    | SendWorkflowEventAction
    | SendNotificationAction
    | CreateTaskAction,
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
    "AutomationRuleSpec",
    "AutomationRuleSpecWithMeta",
    "CloseLinkedSpecTasksAction",
    "Condition",
    "ConditionGroup",
    "CreateTaskAction",
    "EntitySnapshot",
    "ExecutionResult",
    "SendNotificationAction",
    "SendWorkflowEventAction",
    "SetAssigneeAction",
    "SetFieldAction",
    "SetPriorityAction",
    "SetStatusAction",
    "TriggerSpec",
]
