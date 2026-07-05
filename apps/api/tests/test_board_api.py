"""Integration tests for the board router (Task 1.5 fills ``/board/*``).

These exercise the real handlers wired to a fresh :class:`InMemoryBoardService`
per test (via dependency override), proving the route layer round-trips DTOs and
maps domain errors (cycle -> 409, missing -> 404, illegal transition -> 409).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.routers.board import get_board_service
from forge_board import InMemoryBoardService

PROJECT = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    # One fresh, isolated service shared across requests within a single test.
    service = InMemoryBoardService()
    app.dependency_overrides[get_board_service] = lambda: service
    with TestClient(app) as c:
        yield c


def _create_task(client: TestClient, title: str = "Implement login") -> dict[str, Any]:
    resp = client.post("/board/tasks", json={"title": title, "project_id": PROJECT})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_get_task(client: TestClient) -> None:
    created = _create_task(client)
    assert created["key"] == "TASK-1"
    assert created["status"] == "backlog"
    fetched = client.get(f"/board/tasks/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Implement login"


def test_get_missing_task_is_404(client: TestClient) -> None:
    resp = client.get(f"/board/tasks/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_set_status_valid_and_invalid(client: TestClient) -> None:
    task = _create_task(client)
    ok = client.post(f"/board/tasks/{task['id']}/status", json={"status": "ready"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "ready"

    bad = client.post(f"/board/tasks/{task['id']}/status", json={"status": "done"})
    assert bad.status_code == 409


def test_list_tasks_with_filter(client: TestClient) -> None:
    t1 = _create_task(client, "Implement OAuth")
    _create_task(client, "Fix flaky test")
    client.post(f"/board/tasks/{t1['id']}/status", json={"status": "ready"})

    by_text = client.get("/board/tasks", params={"text": "oauth"})
    assert by_text.status_code == 200
    assert len(by_text.json()) == 1

    by_status = client.get("/board/tasks", params={"status": "ready"})
    assert [t["id"] for t in by_status.json()] == [t1["id"]]


def test_bulk_update(client: TestClient) -> None:
    t1 = _create_task(client, "a")
    t2 = _create_task(client, "b")
    resp = client.post(
        "/board/tasks/bulk",
        json=[
            {"task_id": t1["id"], "status": "ready", "priority": "urgent"},
            {"task_id": t2["id"], "status": "ready"},
        ],
    )
    assert resp.status_code == 200
    assert {t["status"] for t in resp.json()} == {"ready"}


def test_dependency_cycle_is_409(client: TestClient) -> None:
    a = _create_task(client, "a")
    b = _create_task(client, "b")
    ok = client.post(f"/board/tasks/{a['id']}/dependencies", json={"depends_on_id": b["id"]})
    assert ok.status_code == 200
    assert b["id"] in ok.json()["depends_on"]

    cycle = client.post(f"/board/tasks/{b['id']}/dependencies", json={"depends_on_id": a["id"]})
    assert cycle.status_code == 409


def test_delete_task(client: TestClient) -> None:
    task = _create_task(client)
    resp = client.delete(f"/board/tasks/{task['id']}")
    assert resp.status_code == 204
    assert client.get(f"/board/tasks/{task['id']}").status_code == 404


def test_epic_crud(client: TestClient) -> None:
    created = client.post("/board/epics", json={"title": "Auth epic", "project_id": PROJECT})
    assert created.status_code == 201
    epic = created.json()
    assert epic["key"] == "EPIC-1"
    assert client.get("/board/epics").json()[0]["title"] == "Auth epic"
    client.delete(f"/board/epics/{epic['id']}")
    assert client.get(f"/board/epics/{epic['id']}").status_code == 404


def test_incident_create(client: TestClient) -> None:
    resp = client.post("/board/incidents", json={"title": "DB down", "project_id": PROJECT})
    assert resp.status_code == 201
    assert resp.json()["key"] == "INC-1"
