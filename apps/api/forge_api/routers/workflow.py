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

import asyncio
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import anyio.from_thread
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from forge_api.auth.rbac import Permission
from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.realtime import approval_events
from forge_api.realtime.broadcaster import Broadcaster, emit_event, get_broadcaster
from forge_api.routers._rbac import require_permission
from forge_api.schemas.red_team import RedTeamGateOut, RedTeamRecordOut, RedTeamTriggerOut
from forge_api.services.approval_service import get_approval_service
from forge_api.services.workflow_gate_hook import (
    GATE_BY_STATE,
    GATE_EVENTS,
    WAITING_STATE_BY_GATE,
    WORKFLOW_RUN_SUBJECT,
    agent_actor_for_run,
    attested_or_blocked,
    event_for_resolution,
    submit_gate_event,
)
from forge_approval import ApprovalService
from forge_approval.models import GateStatus, GateType
from forge_approval.models import Principal as ApprovalPrincipal
from forge_contracts import LineDefinition, WorkflowRun, WorkflowState
from forge_db.models import Task
from forge_db.redteam import RedTeamRepository
from forge_workflow import (
    AmbiguousTransitionError,
    DuplicateRunError,
    GuardFailedError,
    InvalidTransitionError,
    PreconditionError,
    WorkflowDefinitionError,
    WorkflowEngineImpl,
    WorkflowRunNotFoundError,
)
from forge_workflow.default_workflow import DEFAULT_WORKFLOW_NAME
from forge_workflow.editor.errors import UnknownDefinitionError
from forge_workflow.engine import DEFINITION_REVISION_KEY
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
PrincipalDep = Annotated[Principal, Depends(get_current_principal)]


# --------------------------------------------------------------------------- #
# Engine dependency                                                            #
# --------------------------------------------------------------------------- #


def get_workflow_engine(session: SessionDep, principal: PrincipalDep) -> object:
    """The configured engine over the caller's workspace (override in tests).

    ``WORKFLOW_ENGINE_BACKEND=temporal`` selects the durable Temporal engine;
    otherwise the V1 FSM. Both read and write runs through the caller's
    workspace-scoped Postgres store, so a run in another workspace, or none,
    is a 404 (no existence leak) and nothing is lost on restart. Handlers
    commit after a mutation.
    """
    from forge_api.services.workflow_engine import select_workflow_engine

    return select_workflow_engine(session=session, workspace_id=principal.workspace_id)


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
RedTeamFnDep = Annotated[RedTeamFn | None, Depends(get_red_team_fn)]
ApprovalDep = Annotated[ApprovalService, Depends(get_approval_service)]
BroadcasterDep = Annotated[Broadcaster, Depends(get_broadcaster)]

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


class StartTrigger(BaseModel):
    """What started a run (spec K §4.2), kept in the run's context under ``trigger``."""

    model_config = ConfigDict(extra="forbid")

    # No control characters: Postgres JSON refuses NUL, and that must fail here,
    # before the Temporal engine has started a workflow.
    external_ref: str | None = Field(default=None, max_length=512, pattern=r"^[^\x00-\x1f\x7f]*$")


class StartRunRequest(BaseModel):
    """Body for ``POST /workflow/runs``.

    ``definition`` names the workflow to run; omitted, the run is ``default_feature``
    exactly as before definitions could be named.
    """

    task_id: uuid.UUID
    definition: str | None = None
    trigger: StartTrigger | None = None


class TransitionRequest(BaseModel):
    """Body for ``POST /workflow/runs/{run_id}/transition``."""

    event: str


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


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


def _run_id(run: WorkflowRun) -> uuid.UUID:
    """Narrow the contract's optional ``id`` (always set by ``start_run``).

    Mirrors :func:`_run_task_id`: a run reaching the mint gate without an id
    cannot be scanned or recorded (``ensure_red_team_verdict`` requires
    ``workflow_run_id``) — fail loud (500) rather than silently skipping the
    gate. Previously this case was a quiet ``if run.id is not None:`` skip.
    """
    if run.id is None:  # pragma: no cover — start_run always sets it
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="workflow run has no id; cannot red-team scan",
        )
    return run.id


def _task_title(session: Session, task_id: uuid.UUID) -> str | None:
    """The run's task title for the gate inbox (best-effort: a title lookup
    must never be the reason a gate fails to open)."""
    try:
        task = session.get(Task, task_id)
    except Exception:
        session.rollback()
        return None
    return task.title if task is not None else None


def _open_gate_approval(
    service: ApprovalService,
    session: Session,
    broadcaster: Broadcaster,
    *,
    workspace_id: uuid.UUID,
    run: WorkflowRun,
) -> None:
    """Open the approval request for the gate ``run`` just entered (A2).

    The same request the Temporal ``forge.create_approval`` activity opens
    (one pending per ``(run, gate)`` — :meth:`ApprovalService.create` is
    idempotent while pending), requested by the run's agent identity so the
    approval authorizer's rules decide who may resolve it. The gate is then
    announced on ``/ws`` (spec L §6a).
    """
    gate = GATE_BY_STATE[run.current_state]
    run_id = _run_id(run)
    task_id = _run_task_id(run)
    title = _task_title(session, task_id) or f"{gate.value} approval for run {run_id}"
    request = asyncio.run(
        service.create(
            workspace_id=workspace_id,
            gate_type=gate,
            subject_type=WORKFLOW_RUN_SUBJECT,
            subject_id=run_id,
            workflow_run_id=run_id,
            task_id=task_id,
            title=title,
            requested_actor=agent_actor_for_run(run_id),
        )
    )
    # From this worker thread onto the app's loop, where the sockets live.
    anyio.from_thread.run(emit_event, broadcaster, approval_events.requested(request))


def _durable_engine_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="durable_engine_required"
    )


def _unparseable_definition(
    session: Session, *, workspace_id: uuid.UUID, name: str
) -> HTTPException:
    """The refusal for a published revision this release cannot parse.

    A line (raw ``dsl_version: "2"``) waits for the durable engine like any other
    line; anything else is a broken definition.
    """
    from forge_api.services.workflow_engine import published_revision_is_line

    if published_revision_is_line(session, workspace_id=workspace_id, name=name):
        return _durable_engine_required()
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="definition_invalid")


#: The editor's definition-name rule (bundled names follow it too).
_DEFINITION_NAME = re.compile(r"[a-z][a-z0-9_]{1,62}")


def _unknown_definition(name: str) -> HTTPException:
    # The name is not echoed: it is caller input and may not be printable.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown_definition")


def _admit_definition(
    engine: object, session: Session, *, workspace_id: uuid.UUID, name: str
) -> uuid.UUID | None:
    """Refuse, before anything is created or started, a definition this deployment cannot run.

    The name resolves as the engine will resolve it (the workspace's published
    revision over the bundled file); an unknown name never falls back to
    ``default_feature``. Lines need ``forge.StationWorkflow``, which does not exist
    yet (spec K §4.6, ADR-0007). The V1 FSM's gates and status mapping speak only
    :class:`WorkflowState`, so a definition with other states (``incident``) is
    409. ``FeatureWorkflow`` runs only the bundled ``default_feature``, so anything
    else on the Temporal engine, a published fork of it included, would start a
    workflow that can never make progress.

    Returns the admitted revision id (``None`` for a bundled definition).
    """
    from forge_api.services.workflow_engine import build_definition_provider

    if not _DEFINITION_NAME.fullmatch(name):
        # Not a name the editor or a bundle can hold; never sent to the database.
        raise _unknown_definition(name)
    try:
        definition, revision_id, _ = build_definition_provider(session).resolve(
            name, workspace_id=workspace_id
        )
    except UnknownDefinitionError as exc:
        raise _unknown_definition(name) from exc
    except WorkflowDefinitionError as exc:
        raise _unparseable_definition(session, workspace_id=workspace_id, name=name) from exc
    if isinstance(definition, LineDefinition):
        raise _durable_engine_required()
    if isinstance(engine, WorkflowEngineImpl):
        if not all(
            t.from_state in WorkflowState and t.to_state in WorkflowState
            for t in definition.transitions
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="definition_unsupported_by_engine"
            )
    elif revision_id is not None or name != DEFAULT_WORKFLOW_NAME:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="definition_unsupported_by_engine",
        )
    return revision_id


def _started_as_admitted(session: Session, run: WorkflowRun, admitted: uuid.UUID | None) -> bool:
    """True when the V1 start pinned the revision admission resolved, and it is not a line.

    ``engine.start`` resolves the name again, so a revision published after
    admission would otherwise start unchecked.
    """
    from forge_api.services.workflow_engine import build_definition_provider

    pinned = run.context.get(DEFINITION_REVISION_KEY)
    pinned_id = uuid.UUID(str(pinned)) if pinned else None
    if pinned_id != admitted:
        return False
    return pinned_id is None or not isinstance(
        build_definition_provider(session).load_pinned(pinned_id), LineDefinition
    )


def _record_trigger(
    engine: WorkflowEngineImpl, run: WorkflowRun, trigger: dict[str, object]
) -> WorkflowRun:
    """Keep the start trigger in a V1 run's context (same transaction as the run row)."""
    return engine.update_context(_run_id(run), {"trigger": trigger})


@router.post("/runs", response_model=WorkflowRun, status_code=status.HTTP_201_CREATED)
def start_run(
    engine: EngineDep,
    session: SessionDep,
    principal: WriterDep,
    request: StartRunRequest,
) -> WorkflowRun:
    """Start a workflow run for a task in its workflow's initial state.

    The task must be in the caller's workspace (404 otherwise): the run, its
    gate titles and its attestation all read through it. Without ``definition``
    the run is ``default_feature``, as before. With one, the name must resolve in
    the caller's workspace (404 ``unknown_definition``) to a definition this
    engine can run (409 or 503, see :func:`_admit_definition`); the V1 engine pins
    the resolved revision on the run.
    """
    task = session.get(Task, request.task_id)
    if task is None or task.workspace_id != principal.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no task {request.task_id}"
        )
    trigger = request.trigger.model_dump(exclude_none=True) if request.trigger else None
    admitted: uuid.UUID | None = None
    if request.definition is not None:
        admitted = _admit_definition(
            engine, session, workspace_id=principal.workspace_id, name=request.definition
        )
    with _workflow_errors():
        if request.definition is not None and isinstance(engine, WorkflowEngineImpl):
            try:
                run = engine.start(
                    request.task_id, request.definition, workspace_id=principal.workspace_id
                )
            except UnknownDefinitionError as exc:  # archived since admission
                raise _unknown_definition(request.definition) from exc
            except WorkflowDefinitionError as exc:  # republished unparseable since admission
                raise _unparseable_definition(
                    session, workspace_id=principal.workspace_id, name=request.definition
                ) from exc
            if not _started_as_admitted(session, run, admitted):
                session.rollback()
                raise _durable_engine_required()
        elif isinstance(engine, WorkflowEngineImpl):
            run = engine.start(request.task_id)
        else:
            # default_feature on the Temporal engine (the only name it admits). The
            # engine commits the complete row, trigger included, before it starts
            # the workflow (spec K §4.11).
            run = engine.start(request.task_id, trigger=trigger, definition_revision_id=admitted)
        if trigger is not None and isinstance(engine, WorkflowEngineImpl):
            run = _record_trigger(engine, run, trigger)
    session.commit()
    return run


@router.get("/runs/{run_id}", response_model=WorkflowRun)
def get_run(engine: EngineDep, principal: ReaderDep, run_id: uuid.UUID) -> WorkflowRun:
    with _workflow_errors():
        return engine.get_run(run_id)


@router.post("/runs/{run_id}/transition", response_model=WorkflowRun)
def transition(
    engine: EngineDep,
    session: SessionDep,
    principal: WriterDep,
    red_team_fn: RedTeamFnDep,
    approvals: ApprovalDep,
    broadcaster: BroadcasterDep,
    run_id: uuid.UUID,
    request: TransitionRequest,
) -> WorkflowRun:
    """Apply an FSM transition event and return the updated run.

    Red-Team Gate (V1 parity): when a V1 (FSM-engine) transition lands the run
    in ``spec_review`` — the human spec gate, exactly where the Temporal spine
    scans — mint the run's verdict once (idempotent across gate re-entries):
    the configured adversary when wired, an explicit parked-pass otherwise.
    Failure to record is loud (500 via ``_run_id``/``_run_task_id``), never a
    silently skipped gate. The Temporal engine is excluded: its workflow body
    owns the scan. Recovery from a mint failure needs no special-casing: the
    FSM transition itself has already landed (it is a separate store from this
    mint), so the next transition that re-enters ``spec_review`` re-attempts
    the mint (``ensure_red_team_verdict`` is idempotent on "no existing
    record"); alternatively, ``POST /workflow/runs/{run_id}/red-team`` records
    the verdict directly as a manual recovery path.

    Gates (spec K §4.7): the four gate events
    (``spec_approved_by_human`` / ``plan_approved_by_human`` /
    ``review_approved_by_human`` / ``spec_changes_requested``) are refused
    here — they may only arrive from an approval resolution (the gate hook
    submits them engine-direct). And when a V1 transition lands the run in a
    review state, the matching approval request is opened next to the
    red-team mint, exactly where the Temporal spine calls
    ``forge.create_approval``.
    """
    if request.event in GATE_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="gate_events_come_from_approvals",
        )
    with _workflow_errors():
        engine.transition(run_id, request.event)
        run = engine.get_run(run_id)
    session.commit()
    if (
        isinstance(engine, WorkflowEngineImpl)
        and run.current_state == WorkflowState.SPEC_REVIEW.value
    ):
        ensure_red_team_verdict(
            session,
            principal.workspace_id,
            workflow_run_id=_run_id(run),
            task_id=_run_task_id(run),
            phase=_GATEABLE_STATES[run.current_state],
            red_team_fn=red_team_fn,
        )
        session.commit()
    if isinstance(engine, WorkflowEngineImpl) and run.current_state in GATE_BY_STATE:
        _open_gate_approval(
            approvals, session, broadcaster, workspace_id=principal.workspace_id, run=run
        )
    return run


@router.post("/runs/{run_id}/gates/{gate}/sync", response_model=WorkflowRun)
def sync_gate(
    engine: EngineDep,
    session: SessionDep,
    principal: WriterDep,
    approvals: ApprovalDep,
    run_id: uuid.UUID,
    gate: GateType,
) -> WorkflowRun:
    """Re-submit a resolved gate's event if the run is still waiting (A4).

    The recovery path for a gate hook whose engine submit failed after the
    decision was recorded: re-reads the run's latest request for ``gate`` and,
    when it is resolved and the run still sits at that gate, submits the
    event engine-direct with the deciding user as actor. A rejected ``plan`` or
    ``pr`` gate cancels the run, as the hook does (spec K §4.11). Idempotent: a
    run that already advanced or was cancelled (or a gate still pending) is
    returned unchanged. An approved ``pr`` gate merges only on proof of
    attestation: an existing one, else the attestation runs now; when it fails
    the gate is marked blocked and sync answers 409 ``gate_blocked`` (a blocked
    gate is retried the same way). Unknown or foreign runs read as ``404`` (no
    existence leak).
    """
    if gate not in WAITING_STATE_BY_GATE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"gate '{gate.value}' is not a workflow-run gate",
        )
    with _workflow_errors():
        run = engine.get_run(run_id)
    request = asyncio.run(
        approvals.latest_for_gate(
            workspace_id=principal.workspace_id,
            subject_type=WORKFLOW_RUN_SUBJECT,
            subject_id=run_id,
            gate_type=gate,
        )
    )
    if (
        request is not None
        and request.workflow_run_id == run_id
        and request.status is not GateStatus.PENDING
        and run.current_state == WAITING_STATE_BY_GATE[gate]
        and event_for_resolution(gate, request.status) is not None
    ):
        resolver = request.resolver_user_id or principal.user_id
        actor_ref = f"user:{resolver}"
        if gate is GateType.PR and request.status is GateStatus.APPROVED:
            decider = ApprovalPrincipal(kind="user", id=resolver, workspace_id=request.workspace_id)
            if not asyncio.run(attested_or_blocked(approvals, session, request, decider)):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="gate_blocked")
        with _workflow_errors():
            asyncio.run(submit_gate_event(engine, request=request, actor_ref=actor_ref))
        session.commit()
    with _workflow_errors():
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


@router.post(
    "/runs/{run_id}/red-team",
    response_model=RedTeamTriggerOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_run_red_team(
    engine: EngineDep,
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
    "StartTrigger",
    "TransitionRequest",
    "get_red_team_fn",
    "get_workflow_engine",
    "router",
]
