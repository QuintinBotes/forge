"""Writes to an agent run's record, shared by the worker and the run-record API (#238, #256).

The worker's in-process run record (``scf_worker.run_record``) and the routes a run
process reaches its record through (``scf_api.routers.run_record``) store events and the
handoff with the same code, so numbering, redaction and the handoff's format are one.

Events (ADR-0013, spec L seam 3): each is redacted, truncated, numbered and committed on
its own, so a client following ``GET /agent/runs/{id}/events`` sees it immediately.
Writing an event never fails the run: an error is logged and the run carries on.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scf_contracts import AgentRunResult, RunEvent, RunEventDraft, Step, events_for_step
from scf_db.models import AgentRun, AgentRunEvent

__all__ = ["HANDOFF_KEY", "MAX_TEXT", "RunEventSink", "record_handoff"]

logger = logging.getLogger("scf_api.run_record")

#: Longest text kept in an event payload; the full step stays on the run.
MAX_TEXT = 2_000
#: The reserved ``agent_run.output`` key a handoff waits under until close-out
#: records the outcome, which replaces ``output`` and so removes it.
HANDOFF_KEY = "handoff"


def _clean(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= MAX_TEXT else f"{text[: MAX_TEXT - 1]}…"
    if isinstance(value, dict):
        return {str(key): _clean(item, redact) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(item, redact) for item in value]
    return value


class RunEventSink:
    """Numbers, redacts and stores one run's events."""

    def __init__(
        self,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        *,
        session_factory: Callable[[], Session],
        redact: Callable[[str], str],
    ) -> None:
        self._run_id = run_id
        self._workspace_id = workspace_id
        self._factory = session_factory
        self._redact = redact
        self._next: int | None = None

    def on_step(self, step: Step) -> None:
        for draft in events_for_step(step):
            self.emit(draft)

    def emit(self, draft: RunEventDraft, *, idempotency_key: str | None = None) -> bool:
        """Store one event; ``False`` (logged, never raised) when it was not stored.

        Events carrying the same ``idempotency_key`` are stored once: the key names the
        row's id, so a repeat finds it however long after the first it comes.
        """
        event_id = uuid.uuid5(self._run_id, idempotency_key) if idempotency_key else None
        try:
            return self._write(draft, event_id)
        except Exception:
            logger.exception("could not record %s for run %s", draft.type.value, self._run_id)
            return False

    def _write(self, draft: RunEventDraft, event_id: uuid.UUID | None) -> bool:
        for _attempt in range(2):
            with self._factory() as session:
                if self._next is None:
                    last = session.scalar(
                        select(func.max(AgentRunEvent.seq)).where(
                            AgentRunEvent.run_id == self._run_id
                        )
                    )
                    self._next = (last or 0) + 1
                event = RunEvent.from_draft(
                    RunEventDraft(
                        type=draft.type,
                        summary=_clean(draft.summary, self._redact),
                        payload=_clean(draft.payload, self._redact),
                    ),
                    run_id=self._run_id,
                    seq=self._next,
                    ts=datetime.now(UTC),
                )
                session.add(
                    AgentRunEvent(
                        **({"id": event_id} if event_id else {}),
                        workspace_id=self._workspace_id,
                        run_id=event.run_id,
                        seq=event.seq,
                        type=event.type.value,
                        enforcement=event.enforcement,
                        summary=event.summary,
                        payload=event.payload,
                        ts=event.ts,
                    )
                )
                try:
                    session.commit()
                except IntegrityError:  # another writer took this seq, or this event is stored
                    session.rollback()
                    self._next = None
                    if event_id is not None and session.get(AgentRunEvent, event_id) is not None:
                        return True
                    continue
                self._next += 1
                return True
        return False


def record_handoff(
    session: Session, run_id: uuid.UUID, workspace_id: uuid.UUID, result: AgentRunResult
) -> bool:
    """Put ``result`` on the locked run row for close-out; the caller commits.

    ``False`` when the run is not this workspace's, is already closed out, or already
    holds a handoff, which is kept. Never sets a terminal status or ``completed_at``:
    close-out does.
    """
    row = session.get(AgentRun, run_id, with_for_update=True)
    if row is None or row.workspace_id != workspace_id or row.completed_at is not None:
        return False
    output = row.output or {}
    if HANDOFF_KEY in output:
        return False
    row.output = {**output, HANDOFF_KEY: result.model_dump(mode="json")}
    return True
