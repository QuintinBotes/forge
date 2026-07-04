"""API request/response models for the automations router (F21).

Wraps the engine's YAML/JSON-portable :class:`AutomationRuleSpec` for HTTP.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from forge_board.automation import (
    ActionSpec,
    AutomationRuleSpec,
    ConditionGroup,
    TriggerSpec,
)
from forge_contracts.automation import (
    AutomationActionType,
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerType,
    ConditionOp,
)


class AutomationRuleCreate(AutomationRuleSpec):
    """Create body — ``project_id`` comes from the path."""


class AutomationRuleUpdate(BaseModel):
    """Partial update; ``version`` is required for optimistic concurrency."""

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    trigger: TriggerSpec | None = None
    condition: ConditionGroup | None = None
    actions: list[ActionSpec] | None = None
    run_order: int | None = None
    version: int


class RuleWarningModel(BaseModel):
    code: str
    message: str


class AutomationRuleRead(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    description: str | None
    enabled: bool
    trigger: TriggerSpec
    condition: ConditionGroup
    actions: list[ActionSpec]
    run_order: int
    version: int
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    warnings: list[RuleWarningModel] = Field(default_factory=list)


class DryRunRequest(BaseModel):
    task_id: uuid.UUID
    change: dict[str, Any] = Field(default_factory=dict)


class DryRunResult(BaseModel):
    trigger_matched: bool
    condition_result: bool
    planned_actions: list[ActionSpec]
    notes: list[str] = Field(default_factory=list)


class AutomationExecutionRead(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID | None
    rule_version: int
    trigger_type: AutomationTriggerType
    entity_type: AutomationEntityType
    entity_id: uuid.UUID
    status: AutomationExecutionStatus
    condition_result: bool | None
    actions_planned: list[dict]
    action_results: list[dict]
    depth: int
    causation_chain: list[uuid.UUID]
    error: str | None
    latency_ms: int | None
    created_at: datetime


class ExecutionPage(BaseModel):
    items: list[AutomationExecutionRead]
    next_cursor: str | None = None


# --------------------------------------------------------------------------- #
# Catalog (drives the UI builder)                                              #
# --------------------------------------------------------------------------- #


class TriggerCatalogEntry(BaseModel):
    type: AutomationTriggerType
    required_config: list[str] = Field(default_factory=list)


class ActionCatalogEntry(BaseModel):
    type: AutomationActionType
    args: dict[str, Any] = Field(default_factory=dict)


class AutomationCatalog(BaseModel):
    triggers: list[TriggerCatalogEntry]
    condition_fields: list[str]
    condition_ops: list[ConditionOp]
    actions: list[ActionCatalogEntry]


__all__ = [
    "ActionCatalogEntry",
    "AutomationCatalog",
    "AutomationExecutionRead",
    "AutomationRuleCreate",
    "AutomationRuleRead",
    "AutomationRuleUpdate",
    "DryRunRequest",
    "DryRunResult",
    "ExecutionPage",
    "RuleWarningModel",
    "TriggerCatalogEntry",
]
