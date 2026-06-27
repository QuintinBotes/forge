"""The action-executor seam for the automation engine (F21).

The engine is side-effect-free *except* through an :class:`ActionExecutor`. The
concrete executor (DB/board/workflow adapter) lives in the worker; the engine
and dry-run path use the :class:`RecordingActionExecutor` test double, which
plans actions without mutating anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from forge_board.automation.schemas import (
    ActionResult,
    ActionSpec,
    EntitySnapshot,
)
from forge_contracts.automation import AutomationTriggerEnvelope


@dataclass
class ActionContext:
    """Everything an executor needs to perform + attribute one action."""

    rule_id: uuid.UUID
    rule_name: str
    snapshot: EntitySnapshot
    envelope: AutomationTriggerEnvelope
    depth: int
    causation_chain: list[uuid.UUID] = field(default_factory=list)


@runtime_checkable
class ActionExecutor(Protocol):
    """Performs one planned action and returns its :class:`ActionResult`."""

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult: ...


class RecordingActionExecutor:
    """Dry-run / test double: records planned actions, never mutates."""

    def __init__(self) -> None:
        self.planned: list[ActionSpec] = []

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        self.planned.append(action)
        return ActionResult(type=action.type, status="ok", detail={"simulated": True})


__all__ = ["ActionContext", "ActionExecutor", "RecordingActionExecutor"]
