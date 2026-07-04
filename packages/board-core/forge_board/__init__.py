"""Board domain services: epics, tasks, sprints, milestones, incidents.

Public surface (plan Task 1.5):

- :class:`InMemoryBoardService` — a hermetic :class:`forge_contracts.BoardService`
  implementation (CRUD, status workflow, bulk ops, saved filters, dependency
  graph with cycle detection).
- :mod:`forge_board.workflow` — the task status transition table + helpers.
- :mod:`forge_board.graph` — dependency-graph cycle detection utilities.
- :mod:`forge_board.exceptions` — board domain errors (``CycleError`` is the
  shared contract type, re-exported here).
"""

from __future__ import annotations

from forge_board.exceptions import (
    ActiveSprintExistsError,
    BoardError,
    CycleError,
    EntityNotFoundError,
    InvalidStatusTransitionError,
    SprintStateError,
)
from forge_board.service import InMemoryBoardService
from forge_board.sprint_state import SprintStateMachine
from forge_board.velocity import (
    BurndownPoint,
    ScopeEvent,
    SprintTaskSnapshot,
    SprintWindow,
    VelocityResult,
    VelocitySummary,
    compute_burndown,
    compute_velocity,
    compute_velocity_summary,
    ideal_line,
)

__version__ = "0.1.0"

__all__ = [
    "ActiveSprintExistsError",
    "BoardError",
    "BurndownPoint",
    "CycleError",
    "EntityNotFoundError",
    "InMemoryBoardService",
    "InvalidStatusTransitionError",
    "ScopeEvent",
    "SprintStateError",
    "SprintStateMachine",
    "SprintTaskSnapshot",
    "SprintWindow",
    "VelocityResult",
    "VelocitySummary",
    "__version__",
    "compute_burndown",
    "compute_velocity",
    "compute_velocity_summary",
    "ideal_line",
]
