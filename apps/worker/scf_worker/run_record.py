"""The one port an agent run's loop reaches its run record through (#238).

A run process reads the run's stored inputs, appends its events and records its
handoff only through :class:`RunRecord`, so it never opens a database session.
:class:`InProcessRunRecord` is the database-backed implementation the worker and
the tests use; a run process will use an API-backed one (#257) over the run-record
routes (#256), because it never holds the database credential (spec 011 FR-011).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.orm import Session

from scf_api.services.run_record import HANDOFF_KEY, RunEventSink, record_handoff
from scf_contracts import AgentRunResult, RunEventDraft, Step
from scf_db.models import AgentRun

__all__ = ["EXECUTOR_KEY", "HANDOFF_KEY", "InProcessRunRecord", "RunRecord"]

logger = logging.getLogger("scf_worker.run_record")

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
        """Once, before close-out; never sets a terminal status or ``completed_at``."""
        try:
            with self._factory() as session:
                if not record_handoff(session, self._run_id, self._workspace_id, result):
                    return False
                session.commit()
                return True
        except Exception:
            logger.exception("could not record the handoff of run %s", self._run_id)
            return False
