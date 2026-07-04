"""Follow-up task generation (F17): postmortem action items -> board Tasks.

Each :class:`ActionItem` from a postmortem becomes a real board ``Task`` (kind
``bug``/``chore``) in the incident's project via the board service, linked back to
the incident by a label, so postmortem outcomes become tracked engineering work.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from forge_contracts import Priority, TaskDTO, TaskKind
from forge_contracts.incident import ActionItem

__all__ = ["TaskCreator", "create_action_item_tasks"]


class TaskCreator(Protocol):
    """The board capability used to create follow-up tasks (``BoardService``)."""

    def create_task(self, data: TaskDTO) -> TaskDTO: ...


def _kind(value: str) -> TaskKind:
    return TaskKind.BUG if value == "bug" else TaskKind.CHORE


def _priority(value: str) -> Priority:
    try:
        return Priority(value)
    except ValueError:
        return Priority.MEDIUM


def create_action_item_tasks(
    board: TaskCreator,
    *,
    project_id: uuid.UUID,
    action_items: list[ActionItem],
    incident_key: str | None = None,
) -> list[TaskDTO]:
    """Create one board Task per action item; return the created tasks (with ids)."""
    label = f"incident:{incident_key}" if incident_key else "incident"
    created: list[TaskDTO] = []
    for item in action_items:
        task = TaskDTO(
            project_id=project_id,
            kind=_kind(item.kind),
            title=item.title,
            description=item.description,
            priority=_priority(item.priority),
            labels=[label, "postmortem"],
        )
        created.append(board.create_task(task))
    return created
