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


class GuardFailedError(WorkflowError):
    """Raised when a transition rule *exists* for ``(state, event)`` but its
    guard (a ``when``/``condition`` signal) evaluates false (F25).

    Distinct from :class:`InvalidTransitionError` (no rule at all): the event was
    structurally valid from this state, but a precondition signal was not met
    (e.g. the merge gate's ``ci_status_green ∧ spec_validated``). Both map to 409.
    """

    def __init__(self, state: str, event: str, unmet: list[str] | None = None) -> None:
        self.state = state
        self.event = event
        self.unmet = unmet or []
        detail = f"guard failed for event {event!r} from {state!r}"
        if self.unmet:
            detail += f"; unmet: {self.unmet!r}"
        super().__init__(detail)


class PreconditionError(WorkflowError):
    """Raised when a transition's declared ``preconditions`` are not all met (F25).

    Carries the exact unmet items (e.g. ``repo_target_set``) so the API can echo
    them; maps to 409.
    """

    def __init__(self, state: str, event: str, unmet_preconditions: list[str]) -> None:
        self.state = state
        self.event = event
        self.unmet_preconditions = unmet_preconditions
        super().__init__(
            f"unmet preconditions for {event!r} from {state!r}: {unmet_preconditions!r}"
        )


class DuplicateRunError(WorkflowError):
    """Raised when starting a second active run for a task already running (F25)."""

    def __init__(self, identifier: object) -> None:
        self.identifier = identifier
        super().__init__(f"a workflow run already exists for {identifier!r}")


__all__ = [
    "AmbiguousTransitionError",
    "DuplicateRunError",
    "GuardFailedError",
    "InvalidTransitionError",
    "PreconditionError",
    "WorkflowDefinitionError",
    "WorkflowError",
    "WorkflowRunNotFoundError",
]
