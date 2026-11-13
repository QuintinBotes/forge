"""Temporal worker process entrypoint (F25).

``python -m forge_worker.temporal_main`` builds and runs a
``temporalio.worker.Worker`` registering ``forge.FeatureWorkflow`` + every Activity
on the configured task queue. It shares the ``apps/worker`` image/dependency
closure and runs as a *second* process alongside the Celery worker (Celery still
serves FSM-backed runs and non-workflow jobs — §12).

The ``persist_transition`` Activity writes the Postgres ``workflow_run`` projection
through a session-per-call store, keeping it byte-faithful for the board timeline,
run-trace viewer, and audit log. The agent / checks / PR / notify Activity bodies
delegate to the existing slice services where wired; an unwired Activity refuses
(ADR-0007). ``FORGE_ALLOW_FAKE_ACTIVITIES=1`` opts a dev worker into the
stand-ins in :class:`WorkflowActivities`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts import WorkflowRun
from forge_db import create_session_factory
from forge_db.models import AgentRun
from forge_db.models.runs import WorkflowRun as DbWorkflowRun
from forge_worker.dispatch import workspace_mirrors
from forge_worker.run_workspace import remove_run_worktrees
from forge_workflow import WorkflowRunNotFoundError
from forge_workflow.store import SqlAlchemyWorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.client import get_temporal_client
from forge_workflow.temporal.config import TemporalSettings
from forge_workflow.temporal.payloads import CleanupInput
from forge_workflow.temporal.worker import build_temporal_worker

logger = logging.getLogger(__name__)


class SessionPerCallStore:
    """A :class:`WorkflowStore` that opens a fresh committed session per call.

    Suitable for the Temporal worker, where each Activity invocation is an
    independent unit of work. The worker only reads and updates runs the API
    created (``persist_transition``); the run id comes from the workflow's own
    params, so each call scopes the store to that run's workspace.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    @staticmethod
    def _store_for(session: Session, run_id: uuid.UUID | None) -> SqlAlchemyWorkflowStore:
        workspace_id = session.scalar(
            select(DbWorkflowRun.workspace_id).where(DbWorkflowRun.id == run_id)
        )
        if workspace_id is None:
            raise WorkflowRunNotFoundError(run_id)
        return SqlAlchemyWorkflowStore(session, workspace_id=workspace_id)

    def create(self, run: WorkflowRun) -> WorkflowRun:
        raise NotImplementedError("runs are created by the API in their workspace")

    def get(self, run_id: uuid.UUID) -> WorkflowRun:
        with self._factory() as session:
            return self._store_for(session, run_id).get(run_id)

    def update(self, run: WorkflowRun) -> WorkflowRun:
        with self._factory() as session:
            result = self._store_for(session, run.id).update(run)
            session.commit()
            return result

    def find_active_by_task(self, task_id: uuid.UUID) -> WorkflowRun | None:
        raise NotImplementedError("task lookups need a workspace; the worker has none")

    def list_by_task(self, task_id: uuid.UUID) -> list[WorkflowRun]:
        raise NotImplementedError("task lookups need a workspace; the worker has none")


#: Below the workflow's 60 s start-to-close for ``forge.cleanup_worktree``: a slow
#: cleanup reports ``False`` instead of timing out the activity and failing the cancel.
CLEANUP_BUDGET_SECONDS = 45.0


def make_cleanup_fn(factory: sessionmaker[Session]) -> Callable[[CleanupInput], bool]:
    """The ``forge.cleanup_worktree`` body: remove worktrees the run's agents left.

    Idempotent (nothing left is a no-op). Any failure (database, git, disk) is
    logged and reported as ``False``, never raised: a cleanup problem must not
    turn a cancel into a failed run (spec K §4.11).
    """

    def _cleanup(inp: CleanupInput) -> bool:
        deadline = time.monotonic() + CLEANUP_BUDGET_SECONDS
        try:
            with factory() as session:
                agent_runs = session.scalars(
                    select(AgentRun.id).where(
                        AgentRun.workflow_run_id == inp.workflow_run_id,
                        AgentRun.workspace_id == inp.workspace_id,
                    )
                ).all()
            root = workspace_mirrors(inp.workspace_id)
            mirrors = (
                [p for p in root.glob("*/*") if (p / "HEAD").is_file()] if root.is_dir() else []
            )
            remove_run_worktrees(mirrors, agent_runs, deadline=deadline)
        except Exception:
            logger.exception("cleanup_worktree for run %s failed", inp.workflow_run_id)
            return False
        return True

    return _cleanup


def build_activities(factory: sessionmaker[Session] | None = None) -> WorkflowActivities:
    """Build the worker's Activities over a DB-backed projection store.

    ``forge.create_approval`` is wired to the real approval service (spec K
    P0): a Temporal feature run opens its spec/plan/pr gate requests in
    Postgres instead of failing at the first gate. ``forge.cleanup_worktree``
    is bound too, so cancelling a run reaches ``cancelled`` (spec K §4.11).
    """
    from forge_worker.approval_activity import make_create_approval_fn

    session_factory = factory or create_session_factory()
    return WorkflowActivities(
        store=SessionPerCallStore(session_factory),
        approval_fn=make_create_approval_fn(session_factory),
        cleanup_fn=make_cleanup_fn(session_factory),
        allow_fakes=os.environ.get("FORGE_ALLOW_FAKE_ACTIVITIES") == "1",
    )


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
