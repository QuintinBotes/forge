"""Exceptions raised by the workflow engine.

All inherit from :class:`forge_contracts.ForgeError` so callers can catch a
stable, shared base type across the platform.
"""

from __future__ import annotations

from forge_contracts import ForgeError


class WorkflowError(ForgeError):
    """Base class for all workflow-engine errors."""


class WorkflowDefinitionError(WorkflowError):
    """Raised when a workflow DSL document / transition graph is invalid."""


class InvalidTransitionError(WorkflowError):
    """Raised when no transition is enabled for a ``(state, event)`` pair."""

    def __init__(self, state: str, event: str) -> None:
        self.state = state
        self.event = event
        super().__init__(f"no enabled transition from {state!r} on event {event!r}")


class AmbiguousTransitionError(WorkflowError):
    """Raised when more than one transition is enabled for a ``(state, event)``."""

    def __init__(self, state: str, event: str, targets: list[str]) -> None:
        self.state = state
        self.event = event
        self.targets = targets
        super().__init__(
            f"ambiguous transition from {state!r} on event {event!r}: candidates {targets!r}"
        )


class WorkflowRunNotFoundError(WorkflowError):
    """Raised when a workflow run id is not present in the store."""

    def __init__(self, run_id: object) -> None:
        self.run_id = run_id
        super().__init__(f"workflow run not found: {run_id!r}")


__all__ = [
    "AmbiguousTransitionError",
    "InvalidTransitionError",
    "WorkflowDefinitionError",
    "WorkflowError",
    "WorkflowRunNotFoundError",
]
