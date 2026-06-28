"""Sprint velocity worker tasks (F26).

Three Celery tasks wrapping :class:`forge_board.sprint_service.SprintService`:

* ``sprint.snapshot_burndown`` (Beat, daily): upsert one burndown row per active
  sprint for ``snapshot_date = today`` (idempotent via the ``(sprint_id,
  snapshot_date)`` unique constraint).
* ``sprint.recompute_velocity(sprint_id)`` (enqueued by lifecycle ops / the
  scope-capture seam): wholesale recompute of one sprint's rollup (idempotent —
  duplicate deliveries converge).
* ``sprint.reconcile_sprint(sprint_id)`` (enqueued by ``POST /recompute`` /
  ``forge-cli sprint reconcile``): rebuild the rollup **and** replay the burndown
  series day-by-day from ``sprint_scope_event`` (byte-identical to the live path).

The pure functions (``snapshot_active``, ``recompute``, ``reconcile``) are testable
without Celery; the ``*_task`` bodies are the production seams.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session, sessionmaker

from forge_board.sprint_service import SprintService
from forge_worker.celery_app import celery_app

SNAPSHOT_TASK = "sprint.snapshot_burndown"
RECOMPUTE_TASK = "sprint.recompute_velocity"
RECONCILE_TASK = "sprint.reconcile_sprint"


def snapshot_active(session_factory: sessionmaker[Session]) -> int:
    """Snapshot today's burndown for every active sprint (idempotent)."""
    return SprintService(session_factory).snapshot_burndown_for_active()


def recompute(session_factory: sessionmaker[Session], sprint_id: uuid.UUID) -> int | None:
    """Recompute one sprint's velocity rollup."""
    return SprintService(session_factory).recompute_by_id(sprint_id)


def reconcile(session_factory: sessionmaker[Session], sprint_id: uuid.UUID) -> None:
    """Rebuild a sprint's rollup + burndown series from the scope-event log."""
    SprintService(session_factory).reconcile(sprint_id=sprint_id)


def _session_factory() -> sessionmaker[Session]:  # pragma: no cover - prod seam
    from forge_db import create_db_engine, create_session_factory, get_database_url

    return create_session_factory(create_db_engine(get_database_url()))


def snapshot_burndown_task() -> int:  # pragma: no cover - prod seam
    return snapshot_active(_session_factory())


def recompute_velocity_task(sprint_id: str) -> int | None:  # pragma: no cover - prod seam
    return recompute(_session_factory(), uuid.UUID(sprint_id))


def reconcile_sprint_task(sprint_id: str) -> None:  # pragma: no cover - prod seam
    reconcile(_session_factory(), uuid.UUID(sprint_id))


def register_sprint_tasks() -> None:
    """Register the sprint Celery tasks (idempotent)."""
    celery_app.task(name=SNAPSHOT_TASK)(snapshot_burndown_task)
    celery_app.task(name=RECOMPUTE_TASK)(recompute_velocity_task)
    celery_app.task(name=RECONCILE_TASK)(reconcile_sprint_task)


register_sprint_tasks()


__all__ = [
    "RECOMPUTE_TASK",
    "RECONCILE_TASK",
    "SNAPSHOT_TASK",
    "recompute",
    "reconcile",
    "register_sprint_tasks",
    "snapshot_active",
]
