"""Integration tests for the agent router (Phase 2 Task 2.1 wires ``/agent/*``).

Exercises the real handlers wired to a :class:`~forge_agent.AgentRunner` driven by
an offline scripted model client (no live provider calls): run an objective, get
a recorded result, and 404 for an unknown run id.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_api.main import create_app
from forge_api.routers.agent import AgentRunStore, get_agent_runner, get_agent_store


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    runner = AgentRunner(
        ScriptedModelClient(
            responses=[finish_response("done", confidence=0.95)],
            default=finish_response("done", confidence=0.95),
        )
    )
    store = AgentRunStore()
    app.dependency_overrides[get_agent_runner] = lambda: runner
    app.dependency_overrides[get_agent_store] = lambda: store
    with TestClient(app) as c:
        yield c


def test_run_objective_returns_result_with_run_id(client: TestClient) -> None:
    resp = client.post("/agent/runs", json={"objective": "Investigate the bug"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["run_id"]
    assert body["status"] in {"succeeded", "running", "pending"}


def test_run_then_get_round_trips(client: TestClient) -> None:
    created = client.post("/agent/runs", json={"objective": "Do the thing"})
    run_id = created.json()["run_id"]
    fetched = client.get(f"/agent/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id


def test_get_unknown_run_is_404(client: TestClient) -> None:
    resp = client.get(f"/agent/runs/{uuid.uuid4()}")
    assert resp.status_code == 404
