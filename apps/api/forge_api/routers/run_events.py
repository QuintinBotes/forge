"""``GET /agent/runs/{id}/events``: a run's events as Server-Sent Events.

ADR-0013, spec L seam 3. The worker appends events as the run progresses; this
route replays what is stored after the client's last event id, then polls for
more until it has sent a terminal event (``thread.completed`` / ``thread.failed``).
A run that finished without a terminal event (older runs, or a failed event
write) ends the stream once nothing new arrives. Streams are bounded in time;
the client reconnects with ``Last-Event-ID`` and loses nothing.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal
from forge_api.routers._rbac import require_permission
from forge_contracts import TERMINAL_EVENTS, RunEvent, RunEventType
from forge_db.models import AgentRun, AgentRunEvent
from forge_db.models.enums import RunStatus

logger = logging.getLogger("forge_api.run_events")

router = APIRouter(prefix="/agent", tags=["agent"])

#: Seconds between polls for new events.
POLL_SECONDS = 1.0
#: Seconds of silence before a keep-alive comment.
KEEPALIVE_SECONDS = 15.0
#: Longest a single stream stays open; the client reconnects with its last id.
MAX_STREAM_SECONDS = 30 * 60.0
#: Rows read per poll.
_PAGE = 500

_FINISHED = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ESCALATED, RunStatus.CANCELLED}
_TERMINAL_TYPES = {event.value for event in TERMINAL_EVENTS}

SessionFactory = Callable[[], Session]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]


def get_run_event_session_factory() -> SessionFactory:
    """The session factory streams read from (override in tests)."""
    return get_session_factory()


FactoryDep = Annotated[SessionFactory, Depends(get_run_event_session_factory)]


def _run_status(factory: SessionFactory, run_id: uuid.UUID, workspace_id: uuid.UUID) -> str | None:
    with factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None or run.workspace_id != workspace_id:
            return None
        return str(run.status.value if hasattr(run.status, "value") else run.status)


def _fetch(
    factory: SessionFactory, run_id: uuid.UUID, workspace_id: uuid.UUID, after: int
) -> list[AgentRunEvent]:
    with factory() as session:
        stmt = (
            select(AgentRunEvent)
            .where(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.workspace_id == workspace_id,
                AgentRunEvent.seq > after,
            )
            .order_by(AgentRunEvent.seq.asc())
            .limit(_PAGE)
        )
        return list(session.scalars(stmt))


def _message(row: AgentRunEvent) -> str | None:
    try:
        event = RunEvent(
            run_id=row.run_id,
            seq=row.seq,
            type=RunEventType(row.type),
            ts=row.ts,
            summary=row.summary,
            enforcement=row.enforcement,  # type: ignore[arg-type]
            payload=row.payload,
            recorded_at=row.created_at,
            enforceable=row.enforcement == "gated",
        )
    except (ValidationError, ValueError):
        logger.warning("skipping malformed event %s of run %s", row.seq, row.run_id)
        return None
    return f"id: {event.seq}\nevent: {event.type.value}\ndata: {event.model_dump_json()}\n\n"


def _last_id(header: str | None) -> int:
    try:
        return max(int(header), 0) if header else 0
    except ValueError:
        return 0


async def _stream(
    factory: SessionFactory, run_id: uuid.UUID, workspace_id: uuid.UUID, cursor: int
) -> AsyncIterator[str]:
    started = last_sent = time.monotonic()
    while True:
        rows = await run_in_threadpool(_fetch, factory, run_id, workspace_id, cursor)
        for row in rows:
            cursor = row.seq
            message = _message(row)
            if message is not None:
                yield message
                last_sent = time.monotonic()
            if row.type in _TERMINAL_TYPES:
                return
        if not rows:
            run_status = await run_in_threadpool(_run_status, factory, run_id, workspace_id)
            if run_status is None or RunStatus(run_status) in _FINISHED:
                # The worker stores terminal events before the final status, so
                # nothing more is coming.
                return
        now = time.monotonic()
        if now - started >= MAX_STREAM_SECONDS:
            return
        if now - last_sent >= KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            last_sent = now
        if len(rows) < _PAGE:
            await anyio.sleep(POLL_SECONDS)


@router.get(
    "/runs/{run_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
    summary="A run's events as Server-Sent Events (ADR-0013).",
)
async def stream_run_events(
    run_id: uuid.UUID,
    principal: ReaderDep,
    factory: FactoryDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    if await run_in_threadpool(_run_status, factory, run_id, principal.workspace_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no agent run {run_id}")
    cursor = max(after or 0, _last_id(last_event_id))
    return StreamingResponse(
        _stream(factory, run_id, principal.workspace_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
