"""Board-core domain exceptions.

These subclass the shared :class:`forge_contracts.ForgeError` so API callers can
catch a stable, contract-level type. ``CycleError`` is re-exported from the
frozen contracts (plan Task 0.3) — board-core raises *that* type, not a local
duplicate, so ``BoardService.dependency_add`` matches its declared contract.
"""

from __future__ import annotations

from forge_contracts import CycleError, ForgeError


class BoardError(ForgeError):
    """Base class for board-domain errors."""


class EntityNotFoundError(BoardError):
    """Raised when a board entity is referenced by an id that does not exist."""

    def __init__(self, entity: str, entity_id: object) -> None:
        self.entity = entity
        self.entity_id = entity_id
        super().__init__(f"{entity} {entity_id} not found")


class InvalidStatusTransitionError(BoardError):
    """Raised when a task status change violates the workflow rules."""

    def __init__(self, src: object, dst: object) -> None:
        self.src = src
        self.dst = dst
        super().__init__(f"illegal status transition: {src} -> {dst}")


class SprintStateError(BoardError):
    """Raised when a sprint lifecycle transition is illegal (F26)."""

    def __init__(self, frm: object, to: object) -> None:
        self.frm = frm
        self.to = to
        super().__init__(f"illegal sprint transition: {frm} -> {to}")


class ActiveSprintExistsError(BoardError):
    """Raised when starting a sprint while another is already active (F26)."""

    def __init__(self, sprint_id: object) -> None:
        self.sprint_id = sprint_id
        super().__init__(f"an active sprint already exists: {sprint_id}")


__all__ = [
    "ActiveSprintExistsError",
    "BoardError",
    "CycleError",
    "EntityNotFoundError",
    "InvalidStatusTransitionError",
    "SprintStateError",
]
