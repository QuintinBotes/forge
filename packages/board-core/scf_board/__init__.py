"""Board domain services: epics, tasks, sprints, milestones, incidents.

Public surface (plan Task 1.5):

- :class:`InMemoryBoardService` — a hermetic :class:`scf_contracts.BoardService`
  implementation (CRUD, status workflow, bulk ops, saved filters, dependency
  graph with cycle detection).
- :mod:`scf_board.workflow` — the task status transition table + helpers.
- :mod:`scf_board.graph` — dependency-graph cycle detection utilities.
- :mod:`scf_board.exceptions` — board domain errors (``CycleError`` is the
  shared contract type, re-exported here).
"""

from __future__ import annotations

from scf_board.exceptions import (
    ActiveSprintExistsError,
    BoardError,
    CycleError,
    EntityNotFoundError,
    InvalidStatusTransitionError,
    NoProjectError,
    SprintStateError,
)
from scf_board.service import InMemoryBoardService
from scf_board.sprint_state import SprintStateMachine
from scf_board.sql_service import SqlAlchemyBoardService
from scf_board.velocity import (
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
    "NoProjectError",
    "ScopeEvent",
    "SprintStateError",
    "SprintStateMachine",
    "SprintTaskSnapshot",
    "SprintWindow",
    "SqlAlchemyBoardService",
    "VelocityResult",
    "VelocitySummary",
    "__version__",
    "compute_burndown",
    "compute_velocity",
    "compute_velocity_summary",
    "ideal_line",
]
