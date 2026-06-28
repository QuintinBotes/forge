"""Postgres projection tests for the Temporal engine (F25 AC4/11/13).

Exercises the real Postgres code paths via ``SqlAlchemyWorkflowStore``: the
idempotent ``persist_transition`` projection writer, the engine read path served
purely from the projection (never Temporal), the duplicate-active-run guard, and
the partial-unique ``temporal_workflow_id`` index. Parks without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts import RunStatus, WorkflowRun, WorkflowState
from forge_db.base import Base
from forge_db.models import Project, Task, Workspace
from forge_db.models.runs import WorkflowRun as DbWorkflowRun
from forge_workflow.exceptions import DuplicateRunError
from forge_workflow.store import SqlAlchemyWorkflowStore
from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.engine import TemporalWorkflowEngine
from forge_workflow.temporal.payloads import TransitionRecord

pytestmark = [pytest.mark.usefixtures("pg_engine"), pytest.mark.asyncio]


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed_task(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(
        workspace_id=ws.id,
        project_id=project.id,
        key=f"TASK-{uuid.uuid4().hex[:6]}",
        title="temporal task",
    )
    session.add(task)
    session.flush()
    return ws.id, task.id


class _FakeHandle:
    result_run_id = "run-fake-0001"


class _FakeClient:
    def __init__(self) -> None:
        self.starts = 0

    async def start_workflow(self, *args: object, **kwargs: object) -> _FakeHandle:
        self.starts += 1
        return _FakeHandle()


def _new_run(task_id: uuid.UUID, wf_id: str) -> WorkflowRun:
    return WorkflowRun(
        id=uuid.uuid4(),
        task_id=task_id,
        current_state=WorkflowState.CREATED.value,
        status=RunStatus.RUNNING,
        context={
            "engine_backend": "temporal",
            "temporal_workflow_id": wf_id,
            "transitions": [],
        },
    )


async def test_persist_transition_idempotent(factory: sessionmaker[Session]) -> None:
    """AC11 — the same idempotency_key writes one row and returns the same sequence."""
    with factory() as session:
        ws_id, task_id = _seed_task(session)
        store = SqlAlchemyWorkflowStore(session, workspace_id=ws_id)
        run = store.create(_new_run(task_id, f"wf-{uuid.uuid4()}"))
        activities = WorkflowActivities(store=store)

        rec = TransitionRecord(
            workflow_run_id=run.id,
            workspace_id=ws_id,
            from_state=WorkflowState.CREATED,
            to_state=WorkflowState.SPEC_DRAFTING,
            event="generate_spec_draft",
            idempotency_key=f"{run.id}:transition:1",
            effects_dispatched=["generate_spec_draft"],
        )
        seq1 = await activities.persist_transition(rec)
        seq2 = await activities.persist_transition(rec)  # at-least-once redelivery
        assert seq1 == seq2 == 1

        reloaded = store.get(run.id)
        assert len(reloaded.context["transitions"]) == 1
        assert reloaded.current_state == "spec_drafting"


async def test_reads_do_not_touch_temporal(factory: sessionmaker[Session]) -> None:
    """AC13 — get_run/history/list_runs serve from the projection, never Temporal."""

    class _ExplodingClient:
        def __getattr__(self, name: str) -> object:  # any client use is a failure
            raise AssertionError(f"read path touched Temporal client ({name})")

    with factory() as session:
        ws_id, task_id = _seed_task(session)
        store = SqlAlchemyWorkflowStore(session, workspace_id=ws_id)
        run = store.create(_new_run(task_id, f"wf-{uuid.uuid4()}"))
        engine = TemporalWorkflowEngine(
            workspace_id=ws_id, store=store, client=_ExplodingClient()
        )

        fetched = engine.get_run(run.id)
        assert fetched.id == run.id
        assert fetched.context["engine_backend"] == "temporal"
        assert engine.history(run.id) == []
        assert [r.id for r in engine.list_runs(task_id=task_id)] == [run.id]


async def test_duplicate_active_run(factory: sessionmaker[Session]) -> None:
    """AC4 — a second start for a task with a live run is rejected (→ 409)."""
    with factory() as session:
        ws_id, task_id = _seed_task(session)
        store = SqlAlchemyWorkflowStore(session, workspace_id=ws_id)
        engine = TemporalWorkflowEngine(
            workspace_id=ws_id, store=store, client=_FakeClient()
        )
        first = await engine.astart(task_id)
        assert first.context["engine_backend"] == "temporal"

        with pytest.raises(DuplicateRunError):
            await engine.astart(task_id)


async def test_partial_unique_temporal_workflow_id(factory: sessionmaker[Session]) -> None:
    """AC4 (DB) — two runs cannot share a temporal_workflow_id; NULLs are allowed."""
    with factory() as session:
        ws_id, task_id = _seed_task(session)
        wf_id = f"wf-{uuid.uuid4()}"
        session.add(DbWorkflowRun(workspace_id=ws_id, task_id=task_id, temporal_workflow_id=wf_id))
        session.flush()
        session.add(DbWorkflowRun(workspace_id=ws_id, task_id=task_id, temporal_workflow_id=wf_id))
        with pytest.raises(IntegrityError):
            session.flush()
    # Two FSM runs (NULL temporal_workflow_id) must coexist fine.
    with factory() as session:
        ws_id, task_id = _seed_task(session)
        session.add(DbWorkflowRun(workspace_id=ws_id, task_id=task_id))
        session.add(DbWorkflowRun(workspace_id=ws_id, task_id=task_id))
        session.flush()


async def test_engine_backend_defaults_to_fsm(factory: sessionmaker[Session]) -> None:
    """A run created without engine attribution defaults to postgres_fsm."""
    with factory() as session:
        ws_id, task_id = _seed_task(session)
        run = DbWorkflowRun(workspace_id=ws_id, task_id=task_id)
        session.add(run)
        session.flush()
        from forge_db.models.enums import EngineBackend

        assert run.engine_backend == EngineBackend.POSTGRES_FSM
        assert run.temporal_workflow_id is None
