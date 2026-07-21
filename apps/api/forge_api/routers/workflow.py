"""Workflow engine router (Task 1.8 — workflow-engine; wired in Phase 2 Task 2.1).

Serves the FSM workflow surface over HTTP:

* ``POST /workflow/runs``                    — start a run for a task.
* ``GET  /workflow/runs/{run_id}``           — fetch a run.
* ``POST /workflow/runs/{run_id}/transition``— apply an FSM event and return the run.
* ``GET  /workflow/runs/{run_id}/red-team``  — Red-Team Gate verdict + evidence.
* ``POST /workflow/runs/{run_id}/red-team``  — trigger a Red-Team scan for a run.

Handlers delegate to a process-wide :class:`~forge_workflow.WorkflowEngineImpl`
backed by an in-memory run store (the SQLAlchemy-backed store is swapped in
behind the same dependency via ``app.dependency_overrides`` / config). Domain
errors map to HTTP: unknown run -> 404; invalid/ambiguous transition -> 409.

The ``red-team`` GET reads directly off the append-only ``red_team_record``
table (see ``forge_db.redteam``) rather than through the FSM engine: it is
workspace-scoped on the row itself (``run_id`` is the same
``WorkflowParams.workflow_run_id`` the Temporal ``FeatureWorkflow`` scans before
the human spec gate — see ``forge_workflow.temporal.workflows``), so it needs
no engine/ownership lookup and degrades safely (empty history, no leak) for an
unscanned or foreign run.

Red-Team Gate, V1 parity (Task 20): the V1 FSM is a plain transition graph with
no gate hooks, and this router is its only production driver — so when a V1
transition lands a run in ``spec_review`` (the human spec gate, exactly where
the Temporal spine scans), the handler mints the run's verdict once via the
shared :func:`forge_workflow.red_team_gate.ensure_red_team_verdict`: the
configured adversary when one is wired (:func:`get_red_team_fn`), an explicit
parked-pass otherwise — park-don't-fake, never silent. The Temporal spine keeps
owning its own scan inside ``FeatureWorkflow.run`` (the mint is V1-engine-only).
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
from forge_api.schemas.red_team import RedTeamGateOut, RedTeamRecordOut, RedTeamTriggerOut
from forge_contracts import WorkflowRun, WorkflowState
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
from forge_workflow.red_team_gate import (
    RedTeamFn,
    ensure_red_team_verdict,
    run_and_record_red_team,
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


def get_red_team_fn() -> RedTeamFn | None:
    """The configured Red-Team adversary harness (override in tests / deploys via DI).

    ``None`` (the default — no adversary model/sandbox is wired in the API
    process) makes every V1/triggered scan an EXPLICIT parked-pass
    (``kind="parked"``, evidence naming the reason) — park-don't-fake, mirroring
    the Temporal activity's ``_default_red_team``. A real deployment overrides
    this with a callable that runs the heterogeneous, sandboxed adversary
    (``forge_coordinator.red_team.run_red_team``).
    """
    return None


EngineDep = Annotated[WorkflowEngineImpl, Depends(get_workflow_engine)]
OwnershipDep = Annotated[WorkflowOwnership, Depends(get_workflow_ownership)]
RedTeamFnDep = Annotated[RedTeamFn | None, Depends(get_red_team_fn)]

#: Run states at which a red-team scan may be triggered, mapped to the gate
#: phase the scan runs before (mirrors ``RedTeamInput.phase``: spec | pr).
_GATEABLE_STATES: dict[str, str] = {
    WorkflowState.SPEC_REVIEW.value: "spec",
    WorkflowState.PR_OPENED.value: "pr",
    WorkflowState.AWAITING_REVIEW.value: "pr",
}


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


def _run_task_id(run: WorkflowRun) -> uuid.UUID:
    """Narrow the contract's optional ``task_id`` (always set by ``start_run``).

    A run without one cannot be red-team scanned (``RedTeamInput.task_id`` is
    required) — fail loud rather than silently skipping the gate.
    """
    if run.task_id is None:  # pragma: no cover — start_run always sets it
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"workflow run {run.id} has no task_id; cannot red-team scan",
        )
    return run.task_id


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
    session: SessionDep,
    principal: WriterDep,
    red_team_fn: RedTeamFnDep,
    run_id: uuid.UUID,
    request: TransitionRequest,
) -> WorkflowRun:
    """Apply an FSM transition event and return the updated run.

    Red-Team Gate (V1 parity): when a V1 (FSM-engine) transition lands the run
    in ``spec_review`` — the human spec gate, exactly where the Temporal spine
    scans — mint the run's verdict once (idempotent across gate re-entries):
    the configured adversary when wired, an explicit parked-pass otherwise.
    Failure to record is loud (500), never a silently skipped gate. The
    Temporal engine is excluded: its workflow body owns the scan.
    """
    _require_owned(ownership, run_id, principal.workspace_id)
    with _workflow_errors():
        engine.transition(run_id, request.event)
        run = engine.get_run(run_id)
    if (
        isinstance(engine, WorkflowEngineImpl)
        and run.current_state == WorkflowState.SPEC_REVIEW.value
        and run.id is not None
    ):
        ensure_red_team_verdict(
            session,
            principal.workspace_id,
            workflow_run_id=run.id,
            task_id=_run_task_id(run),
            phase=_GATEABLE_STATES[run.current_state],
            red_team_fn=red_team_fn,
        )
        session.commit()
    return run


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


@router.post(
    "/runs/{run_id}/red-team",
    response_model=RedTeamTriggerOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_run_red_team(
    engine: EngineDep,
    ownership: OwnershipDep,
    session: SessionDep,
    principal: WriterDep,
    red_team_fn: RedTeamFnDep,
    run_id: uuid.UUID,
) -> RedTeamTriggerOut:
    """Trigger a Red-Team scan for a run and append its verdict to the history.

    Runs the configured adversary when one is wired (:func:`get_red_team_fn`);
    otherwise records an EXPLICIT parked-pass (``kind="parked"``, evidence
    naming the missing adversary) — never disguised as a real adversarial pass.
    Each trigger appends a fresh ``red_team_record`` (a ``blocked`` scan
    followed by a re-triggered ``survived`` one is the documented history the
    GET surface returns). ``409`` when the run is not at a gateable state
    (``spec_review`` for the spec phase; ``pr_opened``/``awaiting_review`` for
    the pr phase); unknown or foreign runs read as ``404`` (no existence leak).
    """
    _require_owned(ownership, run_id, principal.workspace_id)
    with _workflow_errors():
        run = engine.get_run(run_id)
    phase = _GATEABLE_STATES.get(run.current_state)
    if phase is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"run {run_id} is not at a red-team gateable state "
                f"(current_state={run.current_state!r}; gateable: "
                f"{', '.join(sorted(_GATEABLE_STATES))})"
            ),
        )
    row = run_and_record_red_team(
        session,
        principal.workspace_id,
        workflow_run_id=run_id,
        task_id=_run_task_id(run),
        phase=phase,
        red_team_fn=red_team_fn,
        actor_id=principal.user_id,
    )
    session.commit()
    return RedTeamTriggerOut(
        workflow_run_id=run_id,
        record_id=row.id,
        verdict=row.verdict,
        kind=row.kind,
    )


__all__ = [
    "StartRunRequest",
    "TransitionRequest",
    "WorkflowOwnership",
    "get_red_team_fn",
    "get_workflow_engine",
    "get_workflow_ownership",
    "router",
]
