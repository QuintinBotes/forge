"""``GET /workflow/runs/{id}/stations`` (spec K §4.3): a line run's station visits.

Readers get the visits in the order they started; a run in another workspace, or
none, is 404 (no existence leak).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scf_api.deps import Principal
from scf_api.main import create_app
from scf_contracts import RunStatus, UserRole, WorkflowRun
from scf_db.models.runs import WorkflowStationVisit
from scf_workflow.store import SqlAlchemyWorkflowStore

WS = uuid.UUID("00000000-0000-0000-0000-0000000000c6")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-0000000000c7")
REVISION = uuid.UUID("33333333-3333-3333-3333-333333333333")
T0 = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)

VIEWER = Principal(
    user_id=uuid.uuid4(),
    workspace_id=WS,
    role=UserRole.VIEWER,
    email="viewer@acme.test",
    auth_method="test",
    scopes=["*"],
)


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI], workflow_db) -> TestClient:
    app = workflow_db.install(create_app())
    authenticate_app(app, VIEWER)
    return TestClient(app)


def _seed_run(workflow_db, workspace_id: uuid.UUID, *, visits: bool = True) -> uuid.UUID:
    task_id = workflow_db.seed_task(workspace_id)
    with workflow_db.factory() as session:
        store = SqlAlchemyWorkflowStore(session, workspace_id=workspace_id)
        run = store.create(
            WorkflowRun(task_id=task_id, current_state="review", status=RunStatus.RUNNING)
        )
        assert run.id is not None
        # Added out of order: the route orders by when each visit started.
        seeded = (
            (("review", 20, "running"), ("plan", 0, "succeeded"), ("implement", 10, "succeeded"))
            if visits
            else ()
        )
        for state, minutes, status in seeded:
            session.add(
                WorkflowStationVisit(
                    id=uuid.uuid5(run.id, f"{state}#1"),
                    workspace_id=workspace_id,
                    workflow_run_id=run.id,
                    definition_revision_id=REVISION,
                    state=state,
                    visit=1,
                    kind="agent",
                    status=status,
                    outputs={"summary": state},
                    tokens_used=10,
                    priced=False,
                    started_at=T0 + timedelta(minutes=minutes),
                )
            )
        session.commit()
        return run.id


def test_a_reader_gets_the_visits_in_order(client: TestClient, workflow_db) -> None:
    run_id = _seed_run(workflow_db, WS)
    resp = client.get(f"/workflow/runs/{run_id}/stations")
    assert resp.status_code == 200, resp.text
    visits = resp.json()
    assert [(v["state"], v["status"]) for v in visits] == [
        ("plan", "succeeded"),
        ("implement", "succeeded"),
        ("review", "running"),
    ]
    first = visits[0]
    assert first["id"] == str(uuid.uuid5(run_id, "plan#1"))
    assert (first["visit"], first["kind"], first["tokens_used"]) == (1, "agent", 10)
    assert first["definition_revision_id"] == str(REVISION)
    assert first["outputs"] == {"summary": "plan"}
    assert (first["cost_usd"], first["finished_at"]) == (None, None)


def test_a_run_without_visits_has_none(client: TestClient, workflow_db) -> None:
    run_id = _seed_run(workflow_db, WS, visits=False)
    resp = client.get(f"/workflow/runs/{run_id}/stations")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_another_workspaces_run_or_none_is_404(client: TestClient, workflow_db) -> None:
    foreign_run = _seed_run(workflow_db, OTHER_WS)
    for run_id in (foreign_run, uuid.uuid4()):
        resp = client.get(f"/workflow/runs/{run_id}/stations")
        assert resp.status_code == 404, resp.text
        assert "summary" not in resp.text
