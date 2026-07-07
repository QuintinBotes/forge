"""Task status workflow rules (plan Task 1.5: "status workflow rules").

The board ships a default, total transition table over :class:`TaskStatus`.
``set_status``/``bulk_update`` consult it so a task can only move along legal
edges (the spec's "Custom statuses — per-project with workflow policy rules"
default policy). Same-status transitions are treated as idempotent no-ops.
"""

from __future__ import annotations

from forge_board.exceptions import InvalidStatusTransitionError
from forge_contracts import TaskStatus

#: Statuses considered terminal (a task is finished or abandoned). They can be
#: *reopened* but cannot advance further down the normal flow.
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.DONE, TaskStatus.CANCELLED})

#: Default per-status set of legal next statuses. Total over ``TaskStatus``.
TASK_STATUS_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.BACKLOG: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.READY_FOR_AGENT,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.READY_FOR_AGENT,
            TaskStatus.IN_PROGRESS,
            TaskStatus.BACKLOG,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.READY_FOR_AGENT: frozenset(
        {
            TaskStatus.IN_PROGRESS,
            TaskStatus.READY,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.IN_PROGRESS: frozenset(
        {
            TaskStatus.IN_REVIEW,
            TaskStatus.BLOCKED,
            TaskStatus.READY,
            TaskStatus.DONE,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.IN_REVIEW: frozenset(
        {
            TaskStatus.DONE,
            TaskStatus.IN_PROGRESS,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.READY_FOR_AGENT,
            TaskStatus.IN_PROGRESS,
            TaskStatus.BACKLOG,
            TaskStatus.CANCELLED,
        }
    ),
    # Terminal states may only be reopened.
    TaskStatus.DONE: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.BACKLOG}),
}


def allowed_transitions(src: TaskStatus) -> frozenset[TaskStatus]:
    """Return the set of legal next statuses from ``src``."""
    return TASK_STATUS_TRANSITIONS.get(src, frozenset())


def can_transition(src: TaskStatus, dst: TaskStatus) -> bool:
    """True if moving from ``src`` to ``dst`` is legal (same status is a no-op)."""
    if src == dst:
        return True
    return dst in allowed_transitions(src)


def validate_transition(src: TaskStatus, dst: TaskStatus) -> None:
    """Raise :class:`InvalidStatusTransitionError` if the transition is illegal."""
    if not can_transition(src, dst):
        raise InvalidStatusTransitionError(src, dst)


__all__ = [
    "TASK_STATUS_TRANSITIONS",
    "TERMINAL_STATUSES",
    "allowed_transitions",
    "can_transition",
    "validate_transition",
]
