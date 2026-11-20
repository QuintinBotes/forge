"""Temporal worker process entrypoint (F25).

``python -m scf_worker.temporal_main`` builds and runs a
``temporalio.worker.Worker`` registering ``forge.FeatureWorkflow``, the line
interpreter ``forge.StationWorkflow`` and every Activity on the configured task
queue. It shares the ``apps/worker`` image/dependency closure and runs as a *second*
process alongside the Celery worker (Celery still serves FSM-backed runs and
non-workflow jobs — §12).

The ``persist_transition`` Activity writes the Postgres ``workflow_run`` projection
through a session-per-call store, keeping it byte-faithful for the board timeline,
run-trace viewer, and audit log. The approval and red-team Activities are bound
to real services (``temporal_services``); every other Activity refuses until its
service is bound (ADR-0007, issue #170). ``FORGE_ALLOW_FAKE_ACTIVITIES=1`` opts a
dev worker into the stand-ins in :class:`WorkflowActivities` for the unbound ones.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from scf_contracts import RunStatus, WorkflowRun
from scf_db import create_session_factory
from scf_db.models import AgentRun
from scf_db.models.runs import WorkflowRun as DbWorkflowRun
from scf_db.risk_repository import DbRiskReader
from scf_worker.approval_activity import expire_open_gates
from scf_worker.dispatch import workspace_mirrors
from scf_worker.held_out_drafting import draft_scenarios_service
from scf_worker.line_services import line_services
from scf_worker.run_workspace import remove_run_worktrees
from scf_worker.tasks.pm_dispatch import post_workflow_writeback
from scf_worker.temporal_services import (
    agent_run_service,
    approval_service,
    guard_inputs_service,
    notify_service,
    open_pr_service,
    red_team_service,
    run_checks_service,
)
from scf_workflow import WorkflowRunNotFoundError
from scf_workflow.store import SqlAlchemyWorkflowStore
from scf_workflow.temporal.activities import WorkflowActivities
from scf_workflow.temporal.client import get_temporal_client
from scf_workflow.temporal.config import TemporalSettings
from scf_workflow.temporal.payloads import CleanupInput, StationVisitRecord
from scf_workflow.temporal.worker import build_temporal_worker

logger = logging.getLogger(__name__)


class SessionPerCallStore:
    """A :class:`WorkflowStore` that opens a fresh committed session per call.

    Suitable for the Temporal worker, where each Activity invocation is an
    independent unit of work. The worker only reads and updates runs the API
    created (``persist_transition``) and records their station visits
    (``persist_station``); the run id comes from the workflow's own
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
            store = self._store_for(session, run.id)
            result = store.update(run)
            session.commit()
        # The issue a governed run came from hears its state (#278); this never raises.
        post_workflow_writeback(self._factory, result)
        line = result.context.get("dsl_version") == "2"
        if line and result.id is not None and result.status in _FINISHED:
            # A finished line run's worktree goes; its branch stays for the pull request.
            try:
                remove_run_worktrees(_mirrors(store.workspace_id), [result.id])
            except Exception:  # never fail the transition over a cleanup
                logger.exception("removing the worktree of line run %s failed", result.id)
        return result

    def save_station_visit(self, visit: StationVisitRecord) -> None:
        with self._factory() as session:
            self._store_for(session, visit.workflow_run_id).save_station_visit(visit)
            session.commit()

    def list_station_visits(self, run_id: uuid.UUID) -> list[StationVisitRecord]:
        with self._factory() as session:
            return self._store_for(session, run_id).list_station_visits(run_id)

    def find_active_by_task(self, task_id: uuid.UUID) -> WorkflowRun | None:
        raise NotImplementedError("task lookups need a workspace; the worker has none")

    def list_by_task(self, task_id: uuid.UUID) -> list[WorkflowRun]:
        raise NotImplementedError("task lookups need a workspace; the worker has none")


#: A line run in one of these has no further use for its worktree.
_FINISHED = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


def _mirrors(workspace_id: uuid.UUID) -> list[Path]:
    """The workspace's bare mirrors on this worker."""
    root = workspace_mirrors(workspace_id)
    return [p for p in root.glob("*/*") if (p / "HEAD").is_file()] if root.is_dir() else []


#: Below the workflow's 60 s start-to-close for ``forge.cleanup_worktree``: a slow
#: cleanup reports ``False`` instead of timing out the activity and failing the cancel.
CLEANUP_BUDGET_SECONDS = 45.0


def make_cleanup_fn(factory: sessionmaker[Session]) -> Callable[[CleanupInput], bool]:
    """The ``forge.cleanup_worktree`` body, run when a run is cancelled.

    It removes the worktrees the run's agents left and expires the gate requests
    still pending for the run, as whoever cancelled it (spec K §4.11): a request
    the workflow opened after the API's own expiry would otherwise stay open.
    Idempotent (nothing left is a no-op). Any failure (database, git, disk) is
    logged and reported as ``False``, never raised: a cleanup problem must not
    turn a cancel into a failed run.
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
            # A line run's own worktree is on its branch, named for the workflow run (#181).
            run_ids = [*agent_runs, inp.workflow_run_id]
            remove_run_worktrees(_mirrors(inp.workspace_id), run_ids, deadline=deadline)
        except Exception:
            logger.exception("cleanup_worktree for run %s failed", inp.workflow_run_id)
            cleaned = False
        else:
            cleaned = True
        try:
            expire_open_gates(
                factory,
                workflow_run_id=inp.workflow_run_id,
                workspace_id=inp.workspace_id,
                actor=inp.actor,
            )
        except Exception:
            logger.exception("expiring the open gates of run %s failed", inp.workflow_run_id)
            return False
        return cleaned

    return _cleanup


def build_activities(factory: sessionmaker[Session] | None = None) -> WorkflowActivities:
    """Build the worker's Activities over a DB-backed projection store.

    ``forge.create_approval`` is wired to the real approval service (spec K
    P0): a Temporal feature run opens its spec/plan/pr gate requests in
    Postgres instead of failing at the first gate. ``forge.run_red_team_scan``
    records the run's verdict (issue #170). ``forge.cleanup_worktree`` is bound
    too, so cancelling a run reaches ``cancelled`` (spec K §4.11). Issue #250 binds
    ``forge.load_guard_inputs``, ``forge.run_agent`` and ``forge.pause_and_notify``;
    #251 binds ``forge.run_checks`` and #252 the pull request (opened once).
    ``forge.resume_agent`` still refuses, and so does ``forge.run_agent`` for a line's
    agent station. #181 binds a body for every line service in the catalog.
    """
    factory = factory or create_session_factory()
    return WorkflowActivities(
        store=SessionPerCallStore(factory),
        guard_inputs_fn=guard_inputs_service(factory),
        run_agent_fn=agent_run_service(factory),
        notify_fn=notify_service(factory),
        run_checks_fn=run_checks_service(factory, risk=DbRiskReader(factory)),
        open_pr_fn=open_pr_service(factory),
        approval_fn=approval_service(factory),
        red_team_fn=red_team_service(factory),
        draft_scenarios_fn=draft_scenarios_service(factory),
        cleanup_fn=make_cleanup_fn(factory),
        service_bodies=line_services(factory),
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
