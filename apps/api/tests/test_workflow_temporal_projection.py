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

from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_api.services import workflow_engine
from scf_contracts import UserRole, WorkflowState
from scf_db.base import Base
from scf_db.models import Project, Task, Workspace
from scf_db.models.runs import WorkflowRun as DbWorkflowRun
from scf_worker.temporal_main import build_activities
from scf_workflow.store import SqlAlchemyWorkflowStore
from scf_workflow.temporal.payloads import TransitionRecord, WorkflowParams

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

    async def signal(self, _signal: Any, args: list[Any]) -> None:
        self._client.signals.append((self._wf_id, list(args)))


class _FakeTemporalClient:
    """Records started workflows; an Update runs the worker's projection activity."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.activities = build_activities(factory)
        self.started: dict[str, WorkflowParams] = {}
        self.signals: list[tuple[str, list[Any]]] = []

    async def start_workflow(self, _workflow: Any, params: WorkflowParams, **kwargs: Any) -> Any:
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
    assert row.current_state == "spec_drafting"
    assert [t["event"] for t in row.context["transitions"]] == ["generate_spec_draft"]


def test_ownership_is_read_from_the_row_after_the_registry_is_emptied(
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    run_id = _start(client_as(WS))

    rival = client_as(WS2)  # no in-process registry exists: ownership is the row's
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


def test_the_run_read_returns_each_recorded_check(
    factory: sessionmaker[Session], client_as: Callable[[uuid.UUID], TestClient]
) -> None:
    """#172 criterion 4: the evidence forge.run_checks records is on GET /workflow/runs/{id}."""
    client = client_as(WS)
    run_id = _start(client)
    check = {
        "id": "tests",
        "exit_code": 1,
        "timed_out": False,
        "passed": False,
        "started_at": "2026-11-16T10:00:00+00:00",
        "duration_ms": 1200,
        "output_tail": "E assert 1 == 2",
    }
    attempt = {"attempt": 0, "head_commit": "abc", "no_checks_declared": False, "checks": [check]}
    with factory() as session:
        row = session.get(DbWorkflowRun, run_id)
        assert row is not None
        row.context = {**row.context, "checks": [attempt]}
        session.commit()

    fetched = client.get(f"/workflow/runs/{run_id}")

    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["context"]["checks"] == [attempt]


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


def test_a_run_whose_workflow_never_started_is_started_by_the_next_start(
    factory: sessionmaker[Session],
    temporal: _FakeTemporalClient,
    client_as: Callable[[uuid.UUID], TestClient],
) -> None:
    # What a start leaves when it dies between its commit and ``start_workflow``.
    stale = uuid.uuid4()
    with factory() as session:
        session.add(
            DbWorkflowRun(
                id=stale,
                workspace_id=WS,
                task_id=TASK,
                status="running",
                engine_backend="temporal",
                temporal_workflow_id=f"wf-{stale}",
            )
        )
        session.commit()

    assert _start(client_as(WS)) == stale

    assert {wf_id: p.workflow_run_id for wf_id, p in temporal.started.items()} == {
        f"wf-{stale}": stale
    }
    assert [r.id for r in _active_rows(factory)] == [stale]


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ("cancel", "run_cancelled"),
        # Any other event that moves the run off its gate (the fake's Update lands it
        # in spec_drafting), such as a definition's non-gate edge out of a review state.
        ("generate_spec_draft", "run_left_gate_on_generate_spec_draft"),
    ],
)
def test_leaving_a_gate_without_its_approval_expires_the_open_request(
    factory: sessionmaker[Session], temporal: _FakeTemporalClient, event: str, reason: str
) -> None:
    """Spec K §4.11 (#253): a run that leaves a gate other than through the gate's
    approval expires the gate's open request, recorded as whoever moved the run."""
    from scf_api.services.approval_repository_db import SqlAlchemyApprovalRepository
    from scf_api.services.approval_service import get_approval_service
    from scf_approval import ApprovalAuthorizer, ApprovalService, GateRegistry
    from scf_approval.models import GateStatus, GateType

    approvals = ApprovalService(
        SqlAlchemyApprovalRepository(factory), GateRegistry(), ApprovalAuthorizer()
    )
    app = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    mover = uuid.uuid4()
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_approval_service] = lambda: approvals
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=mover,
        workspace_id=WS,
        role=UserRole.MEMBER,
        email="member@acme.test",
        auth_method="test",
        scopes=["*"],
    )
    client = TestClient(app)
    run_id = _start(client)
    # The run waits at its spec gate, with the request forge.create_approval opened.
    with factory() as session:
        row = session.get(DbWorkflowRun, run_id)
        assert row is not None
        row.current_state = "spec_review"
        session.commit()
    request = asyncio.run(
        approvals.create(
            workspace_id=WS,
            gate_type=GateType.SPEC,
            subject_type="workflow_run",
            subject_id=run_id,
            workflow_run_id=run_id,
            task_id=TASK,
            title="T",
            requested_actor=f"agent:{run_id}",
        )
    )

    resp = client.post(f"/workflow/runs/{run_id}/transition", json={"event": event})

    assert resp.status_code == 200, resp.text
    if event == "cancel":  # the workflow is told, with the canceller as the actor
        assert [args for _, args in temporal.signals] == [["cancelled", f"user:{mover}"]]
    stored = asyncio.run(approvals.get(request.id, workspace_id=WS))
    assert stored.status is GateStatus.EXPIRED
    assert stored.gate_payload["expired"] == {"reason": reason, "actor": f"user:{mover}"}
