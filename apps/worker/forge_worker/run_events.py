"""Append a run's events as it happens (ADR-0013, spec L seam 3).

The worker is the single writer of a run's events. Each event is redacted,
truncated, numbered and committed on its own, so a client following
``GET /agent/runs/{id}/events`` sees it immediately. Writing an event never
fails the run: an error is logged and the run carries on.
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

from forge_contracts import (
    AgentRunResult,
    RunEvent,
    RunEventDraft,
    RunEventType,
    RunStatus,
    Step,
    events_for_step,
)
from forge_db.models import AgentRunEvent

__all__ = ["MAX_TEXT", "RunEventSink", "terminal_drafts"]

logger = logging.getLogger("forge_worker.run_events")

#: Longest text kept in an event payload; the full step stays on the run.
MAX_TEXT = 2_000


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

    def emit(self, draft: RunEventDraft) -> None:
        try:
            self._write(draft)
        except Exception:
            logger.exception("could not record %s for run %s", draft.type.value, self._run_id)

    def _write(self, draft: RunEventDraft) -> None:
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
                except IntegrityError:  # another writer took this seq: re-read and retry
                    session.rollback()
                    self._next = None
                    continue
                self._next += 1
                return


def terminal_drafts(result: AgentRunResult) -> list[RunEventDraft]:
    """``artifact.created`` (if a PR opened), ``cost.updated``, then the terminal event."""
    drafts: list[RunEventDraft] = []
    pull = result.artifacts.get("pull_request")
    if isinstance(pull, dict) and pull.get("status") == "opened":
        drafts.append(
            RunEventDraft(
                type=RunEventType.ARTIFACT_CREATED,
                summary=f"Opened pull request #{pull.get('number')}",
                payload={
                    "kind": "pull_request",
                    "url": pull.get("url"),
                    "number": pull.get("number"),
                },
            )
        )
    usage = result.artifacts.get("model_usage")
    if isinstance(usage, dict):
        drafts.append(
            RunEventDraft(
                type=RunEventType.COST_UPDATED,
                summary=f"{usage.get('input_tokens', 0)} in, {usage.get('output_tokens', 0)} out",
                payload=dict(usage),
            )
        )
    if result.status is RunStatus.SUCCEEDED:
        drafts.append(
            RunEventDraft(
                type=RunEventType.THREAD_COMPLETED,
                summary=result.summary or "Run completed",
                payload={"status": result.status.value},
            )
        )
    else:
        drafts.append(
            RunEventDraft(
                type=RunEventType.THREAD_FAILED,
                summary=result.error or f"Run {result.status.value}",
                payload={"status": result.status.value, "error": result.error},
            )
        )
    return drafts
