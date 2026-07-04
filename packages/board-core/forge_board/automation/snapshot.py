"""Build an :class:`EntitySnapshot` from a task-like object (F21).

Pure + duck-typed (reads attributes via ``getattr``) so it works with the
``forge_db`` ``Task`` ORM object without ``forge_board`` importing the DB layer.
"""

from __future__ import annotations

import uuid
from typing import Any

from forge_board.automation.schemas import EntitySnapshot
from forge_contracts.automation import AutomationEntityType

_TASK_FIELDS = (
    "status",
    "priority",
    "assignee_id",
    "kind",
    "epic_id",
    "sprint_id",
    "milestone_id",
    "estimate",
    "spec_id",
)


def _value(raw: Any) -> Any:
    if isinstance(raw, uuid.UUID):
        return str(raw)
    return getattr(raw, "value", raw)


def snapshot_for_task(task: Any, change: dict[str, Any] | None = None) -> EntitySnapshot:
    """Build a snapshot from a task-like object (DB ``Task`` or DTO)."""
    fields: dict[str, Any] = {f: _value(getattr(task, f, None)) for f in _TASK_FIELDS}
    return EntitySnapshot(
        entity_type=AutomationEntityType.TASK,
        entity_id=task.id,
        fields=fields,
        change=dict(change or {}),
    )


__all__ = ["snapshot_for_task"]
