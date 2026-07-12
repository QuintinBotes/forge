"""Workflow engine router (Task 1.8 — workflow-engine; wired in Phase 2 Task 2.1).

Serves the FSM workflow surface over HTTP:

* ``POST /workflow/runs``                    — start a run for a task.
* ``GET  /workflow/runs/{run_id}``           — fetch a run.
* ``POST /workflow/runs/{run_id}/transition``— apply an FSM event and return the run.
* ``GET  /workflow/runs/{run_id}/red-team``  — Red-Team Gate verdict + evidence.

Handlers delegate to a process-wide :class:`~forge_workflow.WorkflowEngineImpl`
backed by an in-memory run store (the SQLAlchemy-backed store is swapped in
behind the same dependency via ``app.dependency_overrides`` / config). Domain
errors map to HTTP: unknown run -> 404; invalid/ambiguous transition -> 409.

The ``red-team`` route reads directly off the append-only ``red_team_record``
table (see ``forge_db.redteam``) rather than through the FSM engine: it is
workspace-scoped on the row itself (``run_id`` is the same
``WorkflowParams.workflow_run_id`` the Temporal ``FeatureWorkflow`` scans before
the human spec gate — see ``forge_workflow.temporal.workflows``), so it needs
no engine/ownership lookup and degrades safely (empty history, no leak) for an
unscanned or foreign run.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from forge_api.auth.rbac import Permission
from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.red_team import RedTeamGateOut, RedTeamRecordOut
from forge_contracts import WorkflowRun
from forge_db.redteam import RedTeamRepository
from forge_workflow import (
    AmbiguousTransitionError,
    DuplicateRunError,
    GuardFailedError,
    InvalidTransitionError,
    PreconditionError,
    WorkflowEngineImpl,
    WorkflowRunNotFoundError,
)

router = APIRouter(
    prefix="/workflow",
    tags=["workflow"],
    dependencies=[Depends(get_current_principal)],
)

# Authorization: starting / transitioning a run mutates state (WRITE); fetching
# a run is READ. A read-only viewer is denied writes.
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
SessionDep = Annotated[Session, Depends(get_db)]


# --------------------------------------------------------------------------- #
# Run ownership (per-workspace tenant isolation)                               #
# --------------------------------------------------------------------------- #


class WorkflowOwnership:
    """Maps a workflow run id to the workspace that started it.

    The process-wide :class:`WorkflowEngineImpl` is shared across tenants and
    ``WorkflowRun`` (a frozen contract) carries no ``workspace_id``; this registry
    scopes every fetch/transition so one tenant cannot read or drive another
    tenant's run. Unknown *or* foreign ids are reported as 404 (no existence leak).
    """

    def __init__(self) -> None:
        self._owner: dict[uuid.UUID, uuid.UUID] = {}

    def record(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        self._owner[run_id] = workspace_id

    def owns(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        return self._owner.get(run_id) == workspace_id


# --------------------------------------------------------------------------- #
# Engine dependency (overridable for tests / Phase-2 DB swap)                  #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _workflow_engine_singleton() -> WorkflowEngineImpl:
    return WorkflowEngineImpl()


@lru_cache(maxsize=1)
def _workflow_ownership_singleton() -> WorkflowOwnership:
    return WorkflowOwnership()


@lru_cache(maxsize=1)
def _temporal_engine_singleton() -> object:
    # F25 — the V2 durable engine, selected by WORKFLOW_ENGINE_BACKEND=temporal.
    # Connects lazily (client_factory); the projection store is in-process here.
    # NOTE: a DB-backed, per-request engine sharing the worker's Postgres
    # projection is the production wiring (parked — see slice notes).
    from forge_api.services.workflow_engine import select_workflow_engine

    return select_workflow_engine()


def get_workflow_engine() -> object:
    """Return the configured workflow engine (override in tests via DI).

    ``WORKFLOW_ENGINE_BACKEND=temporal`` selects the durable Temporal engine;
    otherwise the V1 in-process FSM. Both satisfy the frozen ``WorkflowEngine``
    protocol so the handlers below are engine-agnostic.
    """
    from forge_api.services.workflow_engine import resolve_backend

    if resolve_backend() == "temporal":
        return _temporal_engine_singleton()
    return _workflow_engine_singleton()


def get_workflow_ownership() -> WorkflowOwnership:
    """Return the process-wide run-ownership registry (override in tests via DI)."""
    return _workflow_ownership_singleton()


EngineDep = Annotated[WorkflowEngineImpl, Depends(get_workflow_engine)]
OwnershipDep = Annotated[WorkflowOwnership, Depends(get_workflow_ownership)]


# --------------------------------------------------------------------------- #
# Error mapping + request bodies                                              #
# --------------------------------------------------------------------------- #


@contextmanager
def _workflow_errors() -> Iterator[None]:
    """Translate workflow domain exceptions into HTTP error responses."""
    try:
        yield
    except WorkflowRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        InvalidTransitionError,
        AmbiguousTransitionError,
        GuardFailedError,
        PreconditionError,
        DuplicateRunError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


class StartRunRequest(BaseModel):
    """Body for ``POST /workflow/runs``."""

    task_id: uuid.UUID


class TransitionRequest(BaseModel):
    """Body for ``POST /workflow/runs/{run_id}/transition``."""

    event: str


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


def _require_owned(
    ownership: WorkflowOwnership, run_id: uuid.UUID, workspace_id: uuid.UUID
) -> None:
    """Reject access to a run outside the caller's workspace (404, no leak)."""
    if not ownership.owns(run_id, workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no workflow run {run_id}"
        )


@router.post("/runs", response_model=WorkflowRun, status_code=status.HTTP_201_CREATED)
def start_run(
    engine: EngineDep,
    ownership: OwnershipDep,
    principal: WriterDep,
    request: StartRunRequest,
) -> WorkflowRun:
    """Start a workflow run for a task in the default workflow's initial state."""
    with _workflow_errors():
        run = engine.start(request.task_id)
    if run.id is not None:
        ownership.record(run.id, principal.workspace_id)
    return run


@router.get("/runs/{run_id}", response_model=WorkflowRun)
def get_run(
    engine: EngineDep, ownership: OwnershipDep, principal: ReaderDep, run_id: uuid.UUID
) -> WorkflowRun:
    _require_owned(ownership, run_id, principal.workspace_id)
    with _workflow_errors():
        return engine.get_run(run_id)


@router.post("/runs/{run_id}/transition", response_model=WorkflowRun)
def transition(
    engine: EngineDep,
    ownership: OwnershipDep,
    principal: WriterDep,
    run_id: uuid.UUID,
    request: TransitionRequest,
) -> WorkflowRun:
    """Apply an FSM transition event and return the updated run."""
    _require_owned(ownership, run_id, principal.workspace_id)
    with _workflow_errors():
        engine.transition(run_id, request.event)
        return engine.get_run(run_id)


@router.get("/runs/{run_id}/red-team", response_model=RedTeamGateOut)
def get_run_red_team(
    session: SessionDep, principal: ReaderDep, run_id: uuid.UUID
) -> RedTeamGateOut:
    """The Red-Team Gate verdict + evidence for a run (feeds the approval-gate badge).

    Reads the append-only ``red_team_record`` table directly, scoped to the
    caller's workspace on the row itself — no run-ownership lookup, since a
    survive/block record only ever exists for a run the workspace already owns.
    An unscanned or foreign ``run_id`` both read as ``latest=None`` with an
    empty history (never a 404): the badge simply has nothing to show yet.
    """
    rows = RedTeamRepository(session).get_by_run(principal.workspace_id, run_id)
    records = [RedTeamRecordOut.model_validate(row) for row in rows]
    return RedTeamGateOut(
        workflow_run_id=run_id,
        latest=records[0] if records else None,
        records=records,
    )


__all__ = [
    "StartRunRequest",
    "TransitionRequest",
    "WorkflowOwnership",
    "get_workflow_engine",
    "get_workflow_ownership",
    "router",
]
