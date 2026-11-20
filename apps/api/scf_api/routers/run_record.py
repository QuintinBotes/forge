"""The run record a run process reaches through SCF's API (#256, spec 011 FR-011, FR-013).

One route per method of the run-record port (``scf_worker.run_record.RunRecord``, #238):
read the run's stored inputs, append an event (``emit``; ``on_step`` appends the events
``events_for_step`` makes of a step) and record the handoff. Each accepts only the live
run token of the run in its path (:func:`~scf_api.auth.run_token.require_run_token`) and
refuses every other credential. Events and the handoff are stored by the same code the
worker's in-process record uses (``scf_api.services.run_record``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from scf_api.auth.run_token import RunCaller, audit_run, require_run_token
from scf_api.deps import DbSession
from scf_api.observability.redaction import redact_text
from scf_api.services.run_record import RunEventSink, record_handoff
from scf_contracts import AgentRunResult, RunEventDraft, RunEventType
from scf_db.models import AgentRun

router = APIRouter(prefix="/agent/runs/{run_id}/record", tags=["run-record"])

#: The handoff route's path, for its own body bound (``FORGE_RUN_HANDOFF_MAX_BODY_BYTES``):
#: a handoff carries every step of the run, tool output included.
HANDOFF_PATH = r"/agent/runs/[^/]+/record/handoff"

RunCallerDep = Annotated[RunCaller, Depends(require_run_token)]


def _factory(session: Session) -> Callable[[], Session]:
    """Fresh sessions on the request's database, as the worker's record opens its own."""
    bind = session.get_bind()
    return lambda: Session(bind)


@router.get("/inputs", summary="The run's stored objective (run token only).")
def read_inputs(caller: RunCallerDep, session: DbSession) -> dict[str, Any]:
    row = session.get(AgentRun, caller.run_id)
    return dict(row.inputs or {}) if row is not None else {}


#: What a run process emits itself (its start and its steps). The workflow's
#: ``thread.state`` notices and close-out's ``artifact.created``, ``cost.updated`` and
#: terminal events are SCF's own record: a run that could write them could hide an
#: escalation or end every follower's stream.
RUN_PROCESS_EVENT_TYPES = frozenset(
    {
        RunEventType.THREAD_STARTED,
        RunEventType.AGENT_MESSAGE,
        RunEventType.TOOL_REQUESTED,
        RunEventType.TOOL_COMPLETED,
        RunEventType.PERMISSION_REQUESTED,
        RunEventType.PERMISSION_GRANTED,
        RunEventType.PERMISSION_DENIED,
        RunEventType.GIT_CHANGED,
    }
)


@router.post(
    "/events",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Append one event to the run (run token only).",
)
def append_event(
    draft: RunEventDraft,
    caller: RunCallerDep,
    session: DbSession,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ] = None,
) -> None:
    """Numbered, redacted and truncated by SCF; a repeated ``Idempotency-Key`` stores once.

    Only the draft's type, summary and payload are read: the sequence number, time and
    provenance (``enforcement``, ``observed_by``, ``enforceable``) are SCF's.
    """
    if draft.type not in RUN_PROCESS_EVENT_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{draft.type} events are SCF's own record, not the run's",
        )
    sink = RunEventSink(
        caller.run_id, caller.workspace_id, session_factory=_factory(session), redact=redact_text
    )
    if not sink.emit(draft, idempotency_key=idempotency_key):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the event was not stored; retry it with the same Idempotency-Key",
        )


async def _handoff_body(request: Request, _caller: RunCallerDep) -> AgentRunResult:
    """The handoff, read only once the run token is checked.

    Its body may be far larger than any other route's (``HANDOFF_PATH``), so a caller
    without the run's token must be refused before it is read or parsed.
    """
    try:
        return AgentRunResult.model_validate_json(await request.body())
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc


@router.post(
    "/handoff",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record the run's handoff for close-out, once (run token only).",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/AgentRunResult"}}
            },
        }
    },
)
def record_run_handoff(
    result: Annotated[AgentRunResult, Depends(_handoff_body)],
    caller: RunCallerDep,
    session: DbSession,
) -> None:
    result = result.model_copy(update={"run_id": caller.run_id})
    with _factory(session)() as writer:
        if not record_handoff(writer, caller.run_id, caller.workspace_id, result):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="this run's handoff is already recorded or closed out",
            )
        audit_run(writer, "agent_run.handoff_recorded", caller, actor_type="agent_runner")
        writer.commit()


__all__ = ["HANDOFF_PATH", "router"]
