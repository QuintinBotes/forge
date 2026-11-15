"""Integration tests for the workflow router (Phase 2 Task 2.1 wires ``/workflow/*``).

Exercises the real handlers and the real engine over the Postgres run store
(SQLite here): start a run, fetch it, apply an FSM transition, and map domain
errors (unknown run or task -> 404).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI], workflow_db) -> Iterator[TestClient]:
    app = workflow_db.install(create_app())
    authenticate_app(app)
    with TestClient(app) as c:
        c.seed_task = workflow_db.seed_task  # type: ignore[attr-defined]
        yield c


def _start(client: TestClient) -> dict:
    task_id = client.seed_task()  # type: ignore[attr-defined]
    resp = client.post("/workflow/runs", json={"task_id": str(task_id)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_start_run_initial_state(client: TestClient) -> None:
    run = _start(client)
    assert run["current_state"] == "created"
    assert run["status"] == "running"
    assert run["id"]


def test_get_run(client: TestClient) -> None:
    run = _start(client)
    fetched = client.get(f"/workflow/runs/{run['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == run["id"]


def test_get_missing_run_is_404(client: TestClient) -> None:
    resp = client.get(f"/workflow/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_transition_advances_state(client: TestClient) -> None:
    run = _start(client)
    resp = client.post(
        f"/workflow/runs/{run['id']}/transition",
        json={"event": "generate_spec_draft"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_state"] == "spec_drafting"


def test_transition_unknown_run_is_404(client: TestClient) -> None:
    resp = client.post(
        f"/workflow/runs/{uuid.uuid4()}/transition", json={"event": "generate_spec_draft"}
    )
    assert resp.status_code == 404


def test_start_with_an_unknown_or_foreign_task_is_404(client: TestClient) -> None:
    foreign = client.seed_task(uuid.uuid4())  # type: ignore[attr-defined]
    for task_id in (uuid.uuid4(), foreign):
        resp = client.post("/workflow/runs", json={"task_id": str(task_id)})
        assert resp.status_code == 404, resp.text


def test_runs_persist_across_engine_instances(client: TestClient) -> None:
    """Each request builds its own engine: the run lives in the store."""
    run = _start(client)
    client.post(f"/workflow/runs/{run['id']}/transition", json={"event": "generate_spec_draft"})
    fetched = client.get(f"/workflow/runs/{run['id']}")
    assert fetched.json()["current_state"] == "spec_drafting"
