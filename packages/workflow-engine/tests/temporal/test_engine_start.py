"""Temporal start commits the run row before it starts the workflow (spec K §4.11).

A fake client stands in for Temporal; the store is a file-backed SQLite database so
a second connection sees only what the engine's session has committed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from scf_contracts import RunStatus
from scf_db.base import Base
from scf_db.models import Project, Task, Workspace
from scf_db.models.runs import WorkflowRun as DbWorkflowRun
from scf_workflow.exceptions import DuplicateRunError
from scf_workflow.store import SqlAlchemyWorkflowStore
from scf_workflow.temporal.engine import TemporalWorkflowEngine
from scf_workflow.temporal.payloads import WorkflowParams

pytestmark = pytest.mark.asyncio


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _seed_task(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as s:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
        s.add(ws)
        s.flush()
        project = Project(workspace_id=ws.id, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(workspace_id=ws.id, project_id=project.id, key="T-1", title="durable start")
        s.add(task)
        s.commit()
        return ws.id, task.id


class _Handle:
    result_run_id = "temporal-run-1"

    def __init__(self, client: _FakeClient | None = None, wf_id: str = "") -> None:
        self._client = client
        self._wf_id = wf_id

    async def terminate(self, *, reason: str) -> None:
        assert self._client is not None
        self._client.terminated.append((self._wf_id, reason))
        if self._client.terminate_fails is not None:
            raise self._client.terminate_fails


class _FakeClient:
    """Reads the run row on its own connection when ``start_workflow`` is called."""

    def __init__(self, factory: sessionmaker[Session], fail: Exception | None = None) -> None:
        self._factory = factory
        self._fail = fail
        self.seen: DbWorkflowRun | None = None
        self.terminated: list[tuple[str, str]] = []
        self.terminate_fails: Exception | None = None

    async def start_workflow(self, _fn: Any, params: WorkflowParams, **_kw: Any) -> _Handle:
        with self._factory() as other:
            self.seen = other.get(DbWorkflowRun, params.workflow_run_id)
        if self._fail is not None:
            raise self._fail
        return _Handle()

    def get_workflow_handle(self, wf_id: str) -> _Handle:
        return _Handle(self, wf_id)


def _engine(session: Session, ws_id: uuid.UUID, client: _FakeClient) -> TemporalWorkflowEngine:
    return TemporalWorkflowEngine(
        workspace_id=ws_id,
        store=SqlAlchemyWorkflowStore(session, workspace_id=ws_id),
        client=client,  # type: ignore[arg-type]
        commit=session.commit,
    )


async def test_the_run_row_is_committed_with_its_trigger_before_start(
    factory: sessionmaker[Session],
) -> None:
    ws_id, task_id = _seed_task(factory)
    client = _FakeClient(factory)
    revision = uuid.uuid4()

    with factory() as session:
        run = await _engine(session, ws_id, client).astart(
            task_id,
            trigger={"external_ref": "linear:ENG-1"},
            definition_revision_id=revision,
        )

    seen = client.seen
    assert seen is not None, "start_workflow ran before the run row was committed"
    assert seen.id == run.id
    assert seen.context["trigger"] == {"external_ref": "linear:ENG-1"}
    assert seen.definition_revision_id == revision
    assert seen.temporal_workflow_id == f"wf-{run.id}"
    assert run.status == RunStatus.RUNNING


async def test_a_failed_start_leaves_the_row_failed_and_committed(
    factory: sessionmaker[Session],
) -> None:
    ws_id, task_id = _seed_task(factory)
    client = _FakeClient(factory, fail=RuntimeError("temporal unavailable"))

    with factory() as session, pytest.raises(RuntimeError, match="temporal unavailable"):
        await _engine(session, ws_id, client).astart(task_id)

    assert client.seen is not None
    with factory() as fresh:
        row = fresh.get(DbWorkflowRun, client.seen.id)
        assert row is not None
        assert row.status.value == "failed"
        assert row.completed_at is not None
        assert row.context["start_error"] == "RuntimeError: temporal unavailable"
        # A failed start is terminal, so the task may start a new run.
        assert (
            SqlAlchemyWorkflowStore(fresh, workspace_id=ws_id).find_active_by_task(task_id) is None
        )


async def test_a_failed_start_terminates_the_workflow_the_server_may_have_started(
    factory: sessionmaker[Session],
) -> None:
    """The call can fail after the server accepted it (a lost response): the run row
    is failed, so that orphan must not run on."""
    ws_id, task_id = _seed_task(factory)
    client = _FakeClient(factory, fail=TimeoutError("no StartWorkflowExecution response"))

    with factory() as session, pytest.raises(TimeoutError):
        await _engine(session, ws_id, client).astart(task_id)

    assert client.seen is not None
    assert client.terminated == [(f"wf-{client.seen.id}", "start_failed")]


async def test_a_terminate_that_finds_nothing_keeps_the_start_error(
    factory: sessionmaker[Session],
) -> None:
    ws_id, task_id = _seed_task(factory)
    client = _FakeClient(factory, fail=RuntimeError("temporal unavailable"))
    client.terminate_fails = RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")

    with factory() as session, pytest.raises(RuntimeError, match="temporal unavailable"):
        await _engine(session, ws_id, client).astart(task_id)

    assert len(client.terminated) == 1


async def test_a_duplicate_start_terminates_nothing(factory: sessionmaker[Session]) -> None:
    ws_id, task_id = _seed_task(factory)
    client = _FakeClient(factory, fail=WorkflowAlreadyStartedError("wf-x", "FeatureWorkflow"))

    with factory() as session, pytest.raises(DuplicateRunError):
        await _engine(session, ws_id, client).astart(task_id)

    assert client.terminated == []


def _seed_unstarted_run(
    factory: sessionmaker[Session], ws_id: uuid.UUID, task_id: uuid.UUID
) -> uuid.UUID:
    """The row a start leaves when it dies between its commit and ``start_workflow``."""
    run_id = uuid.uuid4()
    with factory() as s:
        s.add(
            DbWorkflowRun(
                id=run_id,
                workspace_id=ws_id,
                task_id=task_id,
                status="running",
                engine_backend="temporal",
                temporal_workflow_id=f"wf-{run_id}",
            )
        )
        s.commit()
    return run_id


@pytest.mark.parametrize("unstarted_row", [False, True], ids=["fresh run", "unstarted run"])
async def test_a_start_whose_workflow_already_exists_leaves_the_row_running(
    factory: sessionmaker[Session], unstarted_row: bool
) -> None:
    """The workflow behind the row exists (another start launched it), so the row is
    live: this start is refused and must not fail the row under that workflow."""
    ws_id, task_id = _seed_task(factory)
    if unstarted_row:
        _seed_unstarted_run(factory, ws_id, task_id)
    client = _FakeClient(factory, fail=WorkflowAlreadyStartedError("wf-x", "FeatureWorkflow"))

    with factory() as session, pytest.raises(DuplicateRunError):
        await _engine(session, ws_id, client).astart(task_id)

    assert client.seen is not None, "start_workflow was never asked"
    with factory() as fresh:
        row = fresh.get(DbWorkflowRun, client.seen.id)
        assert row is not None
        assert row.status.value == "running"
        assert "start_error" not in row.context
    assert client.terminated == []


async def test_a_reusing_start_that_fails_transiently_leaves_the_run_and_its_workflow_alone(
    factory: sessionmaker[Session],
) -> None:
    """The row may belong to a start whose workflow is live but has not reported yet:
    a transient error on this second start must not fail that run or stop its workflow."""
    ws_id, task_id = _seed_task(factory)
    run_id = _seed_unstarted_run(factory, ws_id, task_id)
    client = _FakeClient(
        factory, fail=RPCError("deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")
    )

    with factory() as session, pytest.raises(RPCError):
        await _engine(session, ws_id, client).astart(task_id)

    with factory() as fresh:
        row = fresh.get(DbWorkflowRun, run_id)
        assert row is not None
        assert row.status.value == "running"
        assert "start_error" not in row.context
    assert client.terminated == []


async def test_a_start_that_lands_after_a_concurrent_start_failed_the_row_stops_its_workflow(
    factory: sessionmaker[Session],
) -> None:
    """Another start failed this row (and terminated nothing) just before this
    start's workflow began: the workflow would run behind a failed row, so it is
    terminated and the start refused."""
    ws_id, task_id = _seed_task(factory)
    run_id = _seed_unstarted_run(factory, ws_id, task_id)

    class _FailedMeanwhile(_FakeClient):
        async def start_workflow(self, fn: Any, params: WorkflowParams, **kw: Any) -> _Handle:
            with self._factory() as other:
                row = other.get(DbWorkflowRun, params.workflow_run_id)
                assert row is not None
                row.status = "failed"
                other.commit()
            return await super().start_workflow(fn, params, **kw)

    client = _FailedMeanwhile(factory)

    with factory() as session, pytest.raises(DuplicateRunError):
        await _engine(session, ws_id, client).astart(task_id)

    assert client.terminated == [(f"wf-{run_id}", "start_failed")]
