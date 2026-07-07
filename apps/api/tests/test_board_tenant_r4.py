"""Reproduction + regression tests for the Phase-2 round-4 board tenant-isolation fix.

Bug (2.3-fix-r4): the board router authenticated and RBAC-gated (viewer 403) but
never scoped by workspace. It delegated to a single process-wide
``InMemoryBoardService`` with no ``workspace_id`` concept, so:

* Workspace B's ``GET /board/tasks`` returned Workspace A's tasks (cross-tenant
  read leak), and ``list_epics``/``list_incidents`` returned all tenants' rows.
* A member in B could ``GET``/``PATCH``/``DELETE`` A's task by id (no 404).

The r3 hardening added per-workspace isolation to MCP/knowledge/observability/
approval/agent/workflow/spec but left the board (the primary write surface) open.
These tests prove the board is now isolated per workspace like every other
tenant surface.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.board import BoardServiceRegistry, get_board_registry
from forge_contracts import UserRole

WORKSPACE_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WORKSPACE_B = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _principal(workspace_id: uuid.UUID, role: UserRole = UserRole.ADMIN) -> Principal:
    return Principal(
        user_id=USER_ID,
        workspace_id=workspace_id,
        role=role,
        email="tenant@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def _as(app: FastAPI, workspace_id: uuid.UUID, role: UserRole = UserRole.ADMIN) -> None:
    """Point the app's authentication at a workspace's admin for next requests."""
    app.dependency_overrides[get_current_principal] = lambda: _principal(workspace_id, role)


@pytest.fixture
def app() -> Iterator[FastAPI]:
    application = create_app()
    # One fresh, hermetic per-workspace registry shared across the test's
    # requests, so state never bleeds from the process-wide singleton (and the
    # test is order-independent) while each workspace still gets its own service.
    registry = BoardServiceRegistry()
    application.dependency_overrides[get_board_registry] = lambda: registry
    yield application


def test_list_tasks_does_not_leak_across_workspaces(app: FastAPI) -> None:
    with TestClient(app) as client:
        _as(app, WORKSPACE_A)
        created = client.post("/board/tasks", json={"title": "A-secret-task"})
        assert created.status_code == 201, created.text

        # Workspace A sees its own task.
        a_titles = [t["title"] for t in client.get("/board/tasks").json()]
        assert a_titles == ["A-secret-task"]

        # Workspace B must NOT see A's task (the cross-tenant read leak).
        _as(app, WORKSPACE_B)
        assert client.get("/board/tasks").json() == []


def test_cannot_read_update_or_delete_a_foreign_task_by_id(app: FastAPI) -> None:
    with TestClient(app) as client:
        _as(app, WORKSPACE_A)
        task = client.post("/board/tasks", json={"title": "A-secret-task"}).json()
        tid = task["id"]

        # Workspace B (also an admin, so RBAC passes) is blocked by tenancy: a
        # foreign id must look like it does not exist (404, never 200/204).
        _as(app, WORKSPACE_B)
        assert client.get(f"/board/tasks/{tid}").status_code == 404
        assert client.patch(f"/board/tasks/{tid}", json={"title": "hijacked"}).status_code == 404
        assert (
            client.post(f"/board/tasks/{tid}/status", json={"status": "ready"}).status_code == 404
        )
        assert client.delete(f"/board/tasks/{tid}").status_code == 404

        # A's task is untouched.
        _as(app, WORKSPACE_A)
        still = client.get(f"/board/tasks/{tid}")
        assert still.status_code == 200
        assert still.json()["title"] == "A-secret-task"


def test_epics_and_incidents_are_workspace_scoped(app: FastAPI) -> None:
    with TestClient(app) as client:
        _as(app, WORKSPACE_A)
        epic = client.post("/board/epics", json={"title": "A-epic"})
        assert epic.status_code == 201, epic.text
        inc = client.post("/board/incidents", json={"title": "A-incident"})
        assert inc.status_code == 201, inc.text

        _as(app, WORKSPACE_B)
        assert client.get("/board/epics").json() == []
        assert client.get("/board/incidents").json() == []
        assert client.get(f"/board/epics/{epic.json()['id']}").status_code == 404
        assert client.get(f"/board/incidents/{inc.json()['id']}").status_code == 404


def test_keys_are_assigned_per_workspace(app: FastAPI) -> None:
    # Each workspace has its own monotonic key counter, so two tenants both get
    # ``TASK-1`` — proving they are distinct, isolated services.
    with TestClient(app) as client:
        _as(app, WORKSPACE_A)
        assert client.post("/board/tasks", json={"title": "a"}).json()["key"] == "TASK-1"
        _as(app, WORKSPACE_B)
        assert client.post("/board/tasks", json={"title": "b"}).json()["key"] == "TASK-1"
