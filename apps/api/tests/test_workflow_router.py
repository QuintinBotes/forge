"""Integration tests for the workflow router (Phase 2 Task 2.1 wires ``/workflow/*``).

Exercises the real handlers wired to a fresh :class:`~forge_workflow.WorkflowEngineImpl`
per test: start a run, fetch it, apply an FSM transition, and map domain errors
(unknown run -> 404).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.routers.workflow import get_workflow_engine
from forge_workflow import WorkflowEngineImpl


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    engine = WorkflowEngineImpl()
    app.dependency_overrides[get_workflow_engine] = lambda: engine
    with TestClient(app) as c:
        yield c


def _start(client: TestClient) -> dict:
    resp = client.post("/workflow/runs", json={"task_id": str(uuid.uuid4())})
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
