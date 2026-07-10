"""Build an :class:`EntitySnapshot` from a task-like object (F21).

Pure + duck-typed (reads attributes via ``getattr``) so it works with the
``forge_db`` ``Task`` ORM object without ``forge_board`` importing the DB layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from forge_board.automation.schemas import EntitySnapshot
from forge_contracts.automation import AutomationEntityType
from forge_contracts.enums import TaskStatus

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

#: Terminal statuses that count a subtask as "done" for aggregate conditions.
_TERMINAL_STATUSES: frozenset[str] = frozenset({TaskStatus.DONE.value, TaskStatus.CANCELLED.value})


def _value(raw: Any) -> Any:
    if isinstance(raw, uuid.UUID):
        return str(raw)
    return getattr(raw, "value", raw)


def _subtask_status(item: Any) -> Any:
    """Normalise a subtask element to its comparable status *value*.

    Accepts either a raw status (``str``/enum) or a task-like object carrying a
    ``status`` attribute — the DB-backed caller passes the child tasks' status
    values it read from the ``task_dependency`` graph, while the pure/DTO callers
    pass task-like children directly.
    """
    return _value(getattr(item, "status", item))


def _aggregate_subtasks(subtasks: Sequence[Any]) -> dict[str, Any]:
    """Derive F40 aggregate condition fields over a task's subtasks.

    ``subtasks`` is the resolved child collection (see :func:`snapshot_for_task`);
    each element is a status value or a status-bearing object. When a task has no
    children the aggregate fields are ``0`` / ``all_subtasks_done=True``
    (vacuously: nothing is left open).
    """
    items = list(subtasks or [])
    total = len(items)
    open_count = sum(1 for s in items if _subtask_status(s) not in _TERMINAL_STATUSES)
    return {
        "subtask_count": total,
        "open_subtask_count": open_count,
        "all_subtasks_done": open_count == 0,
    }


def snapshot_for_task(
    task: Any,
    change: dict[str, Any] | None = None,
    *,
    subtasks: Sequence[Any] | None = None,
) -> EntitySnapshot:
    """Build a snapshot from a task-like object (DB ``Task`` or DTO).

    ``subtasks`` carries the entity's children for the F40 aggregate conditions
    (``all_subtasks_done`` / ``subtask_count`` / ``open_subtask_count``). The
    ``Task`` ORM row has no in-model parent/child relationship, so the DB-backed
    worker resolves the children from the ``task_dependency`` graph and passes
    them explicitly. When omitted, the duck-typed ``task.subtasks`` attribute is
    used (DTO carriers / pure callers); absent both, a task has no subtasks.
    """
    fields: dict[str, Any] = {f: _value(getattr(task, f, None)) for f in _TASK_FIELDS}
    if subtasks is None:
        subtasks = getattr(task, "subtasks", None) or []
    fields.update(_aggregate_subtasks(subtasks))
    return EntitySnapshot(
        entity_type=AutomationEntityType.TASK,
        entity_id=task.id,
        fields=fields,
        change=dict(change or {}),
    )


__all__ = ["snapshot_for_task"]
