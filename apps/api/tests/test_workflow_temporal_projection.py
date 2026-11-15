"""The Temporal backend's runs live in the shared ``workflow_run`` projection (#169).

Real handlers and a real ``SqlAlchemyWorkflowStore`` over a file-backed SQLite
database, so the API and the worker's activities use separate connections as
they do in production: the worker only sees a row the API committed. Temporal
itself is a fake client whose Update runs the worker's real
``forge.persist_transition`` activity.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import WorkflowAlreadyStartedError

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.workflow import WorkflowOwnership, get_workflow_ownership
from forge_api.services import workflow_engine
from forge_contracts import UserRole, WorkflowState
from forge_db.base import Base
from forge_db.models import Project, Task, Workspace
from forge_db.models.runs import WorkflowRun as DbWorkflowRun
from forge_worker.temporal_main import build_activities
from forge_workflow.store import SqlAlchemyWorkflowStore
from forge_workflow.temporal.payloads import TransitionRecord, WorkflowParams

WS = uuid.UUID("00000000-0000-0000-0000-0000000001a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000001a2")
TASK = uuid.UUID("00000000-0000-0000-0000-0000000001b1")


def _record(params: WorkflowParams, event: str) -> TransitionRecord:
    return TransitionRecord(
        workflow_run_id=params.workflow_run_id,
        workspace_id=params.workspace_id,
        from_state=WorkflowState.CREATED,
        to_state=WorkflowState.SPEC_DRAFTING,
        event=event,
        idempotency_key=f"{params.workflow_run_id}:{event}",
    )


class _FakeHandle:
    result_run_id = "fake-temporal-run"

    def __init__(self, client: _FakeTemporalClient, wf_id: str) -> None:
        self._client = client
        self._wf_id = wf_id

    async def execute_update(self, _update: Any, event: Any) -> str:
        record = _record(self._client.started[self._wf_id], event.type)
        await self._client.activities.persist_transition(record)
        return record.to_state.value


class _FakeTemporalClient:
    """Records started workflows; an Update runs the worker's projection activity."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.activities = build_activities(factory)
        self.started: dict[str, WorkflowParams] = {}
        self.on_start: Callable[[WorkflowParams], None] = lambda _params: None

    async def start_workflow(self, _workflow: Any, params: WorkflowParams, **kwargs: Any) -> Any:
        if kwargs["id"] in self.started:
            raise WorkflowAlreadyStartedError(
                kwargs["id"], "FeatureWorkflow", run_id=_FakeHandle.result_run_id
            )
        self.on_start(params)
        self.started[kwargs["id"]] = params
        return _FakeHandle(self, kwargs["id"])

    def get_workflow_handle(self, wf_id: str) -> _FakeHandle:
        return _FakeHandle(self, wf_id)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'forge.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=WS2, name="Rival", slug="rival"))
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        s.add(Task(id=TASK, workspace_id=WS, project_id=project.id, key="CORE-1", title="T"))
        s.commit()
    yield sf
    engine.dispose()


@pytest.fixture
def temporal(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> _FakeTemporalClient:
    fake = _FakeTemporalClient(factory)

    async def _get_client(_settings: object = None) -> _FakeTemporalClient:
        return fake

    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    monkeypatch.setattr(workflow_engine, "get_temporal_client", _get_client)
    return fake


@pytest.fixture
def client_as(
    factory: sessionmaker[Session], temporal: _FakeTemporalClient
) -> Callable[[uuid.UUID], TestClient]:
    app = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db

    def _client_as(workspace_id: uuid.UUID) -> TestClient:
        principal = Principal(
            user_id=uuid.uuid4(),
            workspace_id=workspace_id,
            role=UserRole.MEMBER,
            email="member@acme.test",
            auth_method="test",
            scopes=["*"],
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        # A fresh registry on every switch stands in for an API restart.
        app.dependency_overrides[get_workflow_ownership] = WorkflowOwnership
        return TestClient(app)

    return _client_as


def _start(client: TestClient) -> uuid.UUID:
    resp = client.post("/workflow/runs", json={"task_id": str(TASK)})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _row(factory: sessionmaker[Session], run_id: uuid.UUID) -> DbWorkflowRun | None:
    with factory() as session:
        return session.get(DbWorkflowRun, run_id)


def test_start_run_writes_workspace_scoped_row_and_starts_workflow_with_it(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    run_id = _start(client_as(WS))

    row = _row(factory, run_id)
    assert row is not None, "the API did not commit a workflow_run row"
    assert row.workspace_id == WS
    assert row.task_id == TASK
    assert row.engine_backend.value == "temporal"
    (params,) = temporal.started.values()
    assert params.workflow_run_id == run_id
    assert params.workspace_id == WS


def test_worker_persist_transition_updates_the_row_the_api_created(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    run_id = _start(client_as(WS))
    (params,) = temporal.started.values()

    activities = build_activities(factory)
    sequence = asyncio.run(activities.persist_transition(_record(params, "generate_spec_draft")))

    assert sequence == 1
    row = _row(factory, run_id)
    assert row is not None
    assert row.current_state.value == "spec_drafting"
    assert [t["event"] for t in row.context["transitions"]] == ["generate_spec_draft"]


def test_ownership_is_read_from_the_row_after_the_registry_is_emptied(
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    run_id = _start(client_as(WS))

    rival = client_as(WS2)  # also empties the in-process registry
    assert rival.get(f"/workflow/runs/{run_id}").status_code == 404
    foreign = rival.post(
        f"/workflow/runs/{run_id}/transition", json={"event": "generate_spec_draft"}
    )
    assert foreign.status_code == 404

    owner = client_as(WS)
    fetched = owner.get(f"/workflow/runs/{run_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["current_state"] == "created"
    moved = owner.post(f"/workflow/runs/{run_id}/transition", json={"event": "generate_spec_draft"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["current_state"] == "spec_drafting"


@pytest.mark.parametrize(
    ("caller", "task_id"),
    [(WS2, TASK), (WS, uuid.UUID("00000000-0000-0000-0000-0000000001bf"))],
    ids=["another workspace's task", "unknown task"],
)
def test_start_run_rejects_a_task_outside_the_callers_workspace(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
    caller: uuid.UUID,
    task_id: uuid.UUID,
) -> None:
    resp = client_as(caller).post("/workflow/runs", json={"task_id": str(task_id)})

    assert resp.status_code == 404, resp.text
    assert temporal.started == {}
    with factory() as session:
        assert session.query(DbWorkflowRun).count() == 0


def _active_rows(factory: sessionmaker[Session]) -> list[DbWorkflowRun]:
    with factory() as session:
        rows = session.query(DbWorkflowRun).filter(DbWorkflowRun.task_id == TASK).all()
    return [r for r in rows if r.status.value not in {"succeeded", "failed", "cancelled"}]


def _add_unstarted_row(factory: sessionmaker[Session]) -> uuid.UUID:
    """The row a crash between the commit and ``start_workflow`` leaves behind."""
    run_id = uuid.uuid4()
    with factory() as session:
        session.add(
            DbWorkflowRun(
                id=run_id,
                workspace_id=WS,
                task_id=TASK,
                status="running",
                engine_backend="temporal",
                temporal_workflow_id=f"wf-{run_id}",
            )
        )
        session.commit()
    return run_id


def test_concurrent_starts_for_one_task_give_one_run_and_one_workflow(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = client_as(WS)
    read = SqlAlchemyWorkflowStore.find_active_by_task
    winner: list[uuid.UUID] = []
    racing: list[bool] = []

    def _read_then_lose_the_race(self: SqlAlchemyWorkflowStore, task_id: uuid.UUID) -> Any:
        found = read(self, task_id)
        if not racing:
            racing.append(True)
            # A second request starts and commits after this one's read.
            winner.append(_start(client))
        return found

    monkeypatch.setattr(SqlAlchemyWorkflowStore, "find_active_by_task", _read_then_lose_the_race)

    loser = client.post("/workflow/runs", json={"task_id": str(TASK)})

    assert loser.status_code == 409, loser.text
    assert [r.id for r in _active_rows(factory)] == winner
    assert [p.workflow_run_id for p in temporal.started.values()] == winner


def test_the_row_is_committed_before_the_workflow_starts(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    seen: list[DbWorkflowRun | None] = []
    # ``_row`` reads on its own connection, as the worker's first activity does.
    temporal.on_start = lambda params: seen.append(_row(factory, params.workflow_run_id))

    _start(client_as(WS))

    assert seen and seen[0] is not None, "start_workflow ran before the row was committed"


def test_a_failed_workflow_start_keeps_the_row_for_the_next_start_to_reuse(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    def _unavailable(_params: WorkflowParams) -> None:
        raise RuntimeError("temporal unavailable")

    temporal.on_start = _unavailable
    client = client_as(WS)
    with pytest.raises(RuntimeError, match="temporal unavailable"):
        client.post("/workflow/runs", json={"task_id": str(TASK)})

    # The error does not prove the workflow never started, so the row is not freed.
    with factory() as session:
        (kept,) = session.query(DbWorkflowRun).all()
    assert kept.status.value == "running"
    assert kept.temporal_run_id is None
    assert temporal.started == {}

    temporal.on_start = lambda _params: None
    assert _start(client) == kept.id
    assert [r.id for r in _active_rows(factory)] == [kept.id]
    assert len(temporal.started) == 1


def test_a_start_that_failed_after_the_workflow_started_does_not_open_a_second_run(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    def _accepted_then_timed_out(params: WorkflowParams) -> None:
        temporal.started[f"wf-{params.workflow_run_id}"] = params
        raise TimeoutError("deadline exceeded after the server accepted the start")

    temporal.on_start = _accepted_then_timed_out
    client = client_as(WS)
    with pytest.raises(TimeoutError):
        client.post("/workflow/runs", json={"task_id": str(TASK)})
    temporal.on_start = lambda _params: None

    retry = client.post("/workflow/runs", json={"task_id": str(TASK)})

    assert retry.status_code == 409, retry.text
    assert len(temporal.started) == 1
    (row,) = _active_rows(factory)
    assert row.temporal_run_id == _FakeHandle.result_run_id


def test_a_row_whose_workflow_never_started_is_reused_by_the_next_start(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    stale = _add_unstarted_row(factory)

    assert _start(client_as(WS)) == stale

    (params,) = temporal.started.values()
    assert params.workflow_run_id == stale
    row = _row(factory, stale)
    assert row is not None
    assert row.temporal_run_id == _FakeHandle.result_run_id
    assert [r.id for r in _active_rows(factory)] == [stale]


def test_reusing_a_row_whose_workflow_did_start_is_refused_and_keeps_the_row(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    # The workflow started, but the request died before recording its run id.
    stale = _add_unstarted_row(factory)
    temporal.started[f"wf-{stale}"] = WorkflowParams(
        workflow_run_id=stale, task_id=TASK, workspace_id=WS
    )

    resp = client_as(WS).post("/workflow/runs", json={"task_id": str(TASK)})

    assert resp.status_code == 409, resp.text
    assert len(temporal.started) == 1
    row = _row(factory, stale)
    assert row is not None
    assert row.status.value == "running"
    assert row.temporal_run_id == _FakeHandle.result_run_id
