"""Temporal worker process entrypoint (F25).

``python -m forge_worker.temporal_main`` builds and runs a
``temporalio.worker.Worker`` registering ``forge.FeatureWorkflow`` + every Activity
on the configured task queue. It shares the ``apps/worker`` image/dependency
closure and runs as a *second* process alongside the Celery worker (Celery still
serves FSM-backed runs and non-workflow jobs — §12).

The ``persist_transition`` Activity writes the Postgres ``workflow_run`` projection
through a session-per-call store, keeping it byte-faithful for the board timeline,
run-trace viewer, and audit log. The agent / checks / PR / notify Activity bodies
delegate to the existing slice services where wired; until then the safe defaults
in :class:`WorkflowActivities` keep the durable spine runnable (SOFT deps park,
mirroring the FSM engine).
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.orm import Session, sessionmaker

from forge_contracts import WorkflowRun
from forge_db import create_session_factory
from forge_workflow.store import SqlAlchemyWorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.client import get_temporal_client
from forge_workflow.temporal.config import TemporalSettings
from forge_workflow.temporal.worker import build_temporal_worker

# A nil workspace for store ops that do not create rows (get/update are keyed by
# the run's PK, so workspace scoping is irrelevant there; create runs in the API).
_NIL_WORKSPACE = uuid.UUID(int=0)


class SessionPerCallStore:
    """A :class:`WorkflowStore` that opens a fresh committed session per call.

    Suitable for the Temporal worker, where each Activity invocation is an
    independent unit of work. ``persist_transition`` only uses ``get`` + ``update``
    (keyed by run id), so the placeholder workspace id is never consulted.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def _store(
        self, session: Session, workspace_id: uuid.UUID = _NIL_WORKSPACE
    ) -> SqlAlchemyWorkflowStore:
        return SqlAlchemyWorkflowStore(session, workspace_id=workspace_id)

    def create(self, run: WorkflowRun) -> WorkflowRun:
        with self._factory() as session:
            result = self._store(session).create(run)
            session.commit()
            return result

    def get(self, run_id: uuid.UUID) -> WorkflowRun:
        with self._factory() as session:
            return self._store(session).get(run_id)

    def update(self, run: WorkflowRun) -> WorkflowRun:
        with self._factory() as session:
            result = self._store(session).update(run)
            session.commit()
            return result

    def find_active_by_task(self, task_id: uuid.UUID) -> WorkflowRun | None:
        with self._factory() as session:
            return self._store(session).find_active_by_task(task_id)

    def list_by_task(self, task_id: uuid.UUID) -> list[WorkflowRun]:
        with self._factory() as session:
            return self._store(session).list_by_task(task_id)


def build_activities(factory: sessionmaker[Session] | None = None) -> WorkflowActivities:
    """Build the worker's Activities over a DB-backed projection store."""
    return WorkflowActivities(store=SessionPerCallStore(factory or create_session_factory()))


async def run(settings: TemporalSettings | None = None) -> None:
    settings = settings or TemporalSettings()
    client = await get_temporal_client(settings)
    activities = build_activities()
    worker = build_temporal_worker(client, activities, task_queue=settings.temporal_task_queue)
    await worker.run()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    main()
