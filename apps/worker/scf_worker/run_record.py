"""The one port an agent run's loop reaches its run record through (#238).

A run process reads the run's stored inputs, appends its events and records its
handoff only through :class:`RunRecord`, so it never opens a database session.
:class:`InProcessRunRecord` is the database-backed implementation the worker and
the tests use; a run process will use an API-backed one (a follow-up), because it
never holds the database credential (spec 011 FR-011).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.orm import Session

from scf_contracts import AgentRunResult, RunEventDraft, Step
from scf_db.models import AgentRun
from scf_worker.run_events import RunEventSink

__all__ = ["EXECUTOR_KEY", "HANDOFF_KEY", "InProcessRunRecord", "RunRecord"]

logger = logging.getLogger("scf_worker.run_record")

#: The reserved ``agent_run.output`` key a handoff waits under until close-out
#: records the outcome, which replaces ``output`` and so removes it.
HANDOFF_KEY = "handoff"
#: The ``agent_run.output`` key a launched run's executor, unit and close-out claim are
#: kept under (#239); recording the outcome keeps it.
EXECUTOR_KEY = "executor"


class RunRecord(Protocol):
    """Everything the loop reads from or writes to its own run."""

    def inputs(self) -> dict[str, Any] | None:
        """The run's stored objective; ``None`` when the run is not this workspace's."""
        ...

    def emit(self, draft: RunEventDraft) -> bool: ...

    def on_step(self, step: Step) -> None: ...

    def record_handoff(self, result: AgentRunResult) -> bool:
        """Store the loop's result for close-out; ``False`` when it was not stored."""
        ...


class InProcessRunRecord:
    """A :class:`RunRecord` over the database, scoped to one run of one workspace."""

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
        self._events = RunEventSink(
            run_id, workspace_id, session_factory=session_factory, redact=redact
        )

    def inputs(self) -> dict[str, Any] | None:
        with self._factory() as session:
            row = session.get(AgentRun, self._run_id)
            if row is None or row.workspace_id != self._workspace_id:
                return None
            return dict(row.inputs or {})

    def emit(self, draft: RunEventDraft) -> bool:
        return self._events.emit(draft)

    def on_step(self, step: Step) -> None:
        self._events.on_step(step)

    def record_handoff(self, result: AgentRunResult) -> bool:
        """Never sets a terminal status or ``completed_at``: close-out does."""
        try:
            with self._factory() as session:
                row = session.get(AgentRun, self._run_id, with_for_update=True)
                if row is None or row.workspace_id != self._workspace_id:
                    return False
                row.output = {**(row.output or {}), HANDOFF_KEY: result.model_dump(mode="json")}
                session.commit()
                return True
        except Exception:
            logger.exception("could not record the handoff of run %s", self._run_id)
            return False
