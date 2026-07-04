"""Sprint lifecycle state machine (F26).

Pure (no I/O): the allowed-transition table + guards used by the lifecycle
service. ``planned -> active -> completed`` is the happy path; ``planned`` and
``active`` may also be ``cancelled``; ``completed`` / ``cancelled`` are terminal.
"""

from __future__ import annotations

from typing import ClassVar

from forge_board.exceptions import SprintStateError
from forge_contracts.enums import SprintState


class SprintStateMachine:
    """Allowed sprint transitions + a fail-closed guard."""

    ALLOWED: ClassVar[dict[SprintState, set[SprintState]]] = {
        SprintState.PLANNED: {SprintState.ACTIVE, SprintState.CANCELLED},
        SprintState.ACTIVE: {SprintState.COMPLETED, SprintState.CANCELLED},
        SprintState.COMPLETED: set(),
        SprintState.CANCELLED: set(),
    }

    def can_transition(self, frm: SprintState, to: SprintState) -> bool:
        """True iff ``frm -> to`` is a legal lifecycle transition."""
        return to in self.ALLOWED.get(frm, set())

    def assert_transition(self, frm: SprintState, to: SprintState) -> None:
        """Raise :class:`SprintStateError` unless ``frm -> to`` is legal."""
        if not self.can_transition(frm, to):
            raise SprintStateError(frm, to)


__all__ = ["SprintStateMachine"]
