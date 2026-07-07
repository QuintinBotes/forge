"""Reproduction + regression tests for the Phase-2 round-3 security fixes (2.3-fix-r3).

Four real defects in routers that round-2 hardening skipped:

1. **Board router has no RBAC** — a read-only ``viewer`` (and the ``agent-runner``)
   could perform every board write (create/update/delete/status/bulk/dependency).
2. **MCP router has no RBAC and no per-workspace scoping** — a viewer could register
   connections / call tools, and connections leaked across tenants (workspace B
   could enumerate, read, call, and audit workspace A's MCP servers).
3. **Observability audit log + run traces are not workspace-scoped** — any
   authenticated caller could read every tenant's audit entries and fetch any
   run's trace by id.
4. **Knowledge write routes** (``POST /knowledge/index`` / ``/knowledge/sync``)
   were not RBAC-gated — a viewer could mutate the index.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_contracts import UserRole

# Deterministic identities (the tests dir is not an importable package, so these
# mirror conftest rather than importing it).
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
OTHER_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def make_test_principal(
    *,
    role: UserRole = UserRole.ADMIN,
    workspace_id: uuid.UUID = TEST_WORKSPACE_ID,
) -> Principal:
    return Principal(
        user_id=TEST_USER_ID,
        workspace_id=workspace_id,
        role=role,
        email="test-principal@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def _as(app: FastAPI, principal: Principal) -> None:
    """Point the app's authentication at ``principal`` for subsequent requests."""
    app.dependency_overrides[get_current_principal] = lambda: principal


# --------------------------------------------------------------------------- #
# 1. Board router RBAC                                                          #
# --------------------------------------------------------------------------- #


def _board_writes(client: TestClient) -> list[tuple[str, object]]:
    """Every mutating board route, as (label, response) pairs."""
    tid = uuid.uuid4()
    mid = uuid.uuid4()
    return [
        ("create_task", client.post("/board/tasks", json={"title": "t"})),
        (
            "bulk_update",
            client.post("/board/tasks/bulk", json=[{"task_id": str(tid), "status": "ready"}]),
        ),
        ("update_task", client.patch(f"/board/tasks/{tid}", json={"title": "x"})),
        ("delete_task", client.delete(f"/board/tasks/{tid}")),
        (
            "set_status",
            client.post(f"/board/tasks/{tid}/status", json={"status": "ready"}),
        ),
        (
            "add_dependency",
            client.post(
                f"/board/tasks/{tid}/dependencies", json={"depends_on_id": str(uuid.uuid4())}
            ),
        ),
        ("create_epic", client.post("/board/epics", json={"title": "e"})),
        ("update_epic", client.patch(f"/board/epics/{tid}", json={"title": "x"})),
        ("delete_epic", client.delete(f"/board/epics/{tid}")),
        ("create_sprint", client.post("/board/sprints", json={"title": "s"})),
        ("update_sprint", client.patch(f"/board/sprints/{tid}", json={"title": "x"})),
        ("delete_sprint", client.delete(f"/board/sprints/{tid}")),
        ("create_milestone", client.post("/board/milestones", json={"title": "m"})),
        ("update_milestone", client.patch(f"/board/milestones/{mid}", json={"title": "x"})),
        ("delete_milestone", client.delete(f"/board/milestones/{mid}")),
        ("create_incident", client.post("/board/incidents", json={"title": "i"})),
        ("update_incident", client.patch(f"/board/incidents/{tid}", json={"title": "x"})),
        ("delete_incident", client.delete(f"/board/incidents/{tid}")),
    ]


def test_viewer_cannot_perform_any_board_write() -> None:
    app = create_app()
    _as(app, make_test_principal(role=UserRole.VIEWER))
    with TestClient(app) as client:
        for label, resp in _board_writes(client):
            assert resp.status_code == 403, f"{label}: expected 403, got {resp.status_code}"


def test_agent_runner_cannot_perform_board_writes() -> None:
    # agent-runner lacks WRITE: it acts only through policy-gated runs.
    app = create_app()
    _as(app, make_test_principal(role=UserRole.AGENT_RUNNER))
    with TestClient(app) as client:
        assert client.post("/board/tasks", json={"title": "t"}).status_code == 403


def test_viewer_may_still_read_board() -> None:
    app = create_app()
    _as(app, make_test_principal(role=UserRole.VIEWER))
    with TestClient(app) as client:
        assert client.get("/board/tasks").status_code == 200


def test_member_can_write_board() -> None:
    app = create_app()
    _as(app, make_test_principal(role=UserRole.MEMBER))
    with TestClient(app) as client:
        assert client.post("/board/tasks", json={"title": "t"}).status_code == 201


# --------------------------------------------------------------------------- #
# 2. MCP router RBAC + per-workspace scoping                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def mcp_app() -> Iterator[FastAPI]:
    from forge_api.routers.mcp import get_mcp_manager
    from forge_mcp import MCPConnectionManager
    from forge_mcp.testing import sample_transport

    app = create_app()
    manager = MCPConnectionManager(transport_factory=lambda conn: sample_transport())
    app.dependency_overrides[get_mcp_manager] = lambda: manager
    yield app


def _register(client: TestClient, **overrides: object) -> dict[str, object]:
    from forge_mcp.testing import sample_connection

    conn = sample_connection(**overrides)
    resp = client.post("/mcp/connections", json=conn.model_dump(mode="json"))
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_viewer_cannot_register_mcp_connection(mcp_app: FastAPI) -> None:
    _as(mcp_app, make_test_principal(role=UserRole.VIEWER))
    from forge_mcp.testing import sample_connection

    conn = sample_connection(allow_write=True)
    with TestClient(mcp_app) as client:
        resp = client.post("/mcp/connections", json=conn.model_dump(mode="json"))
    assert resp.status_code == 403, resp.text


def test_viewer_cannot_call_mcp_tool(mcp_app: FastAPI) -> None:
    _as(mcp_app, make_test_principal(role=UserRole.ADMIN))
    with TestClient(mcp_app) as client:
        _register(client)
        _as(mcp_app, make_test_principal(role=UserRole.VIEWER))
        resp = client.post(
            "/mcp/connections/confluence-engineering/tools/call",
            json={"name": "search_pages", "arguments": {"q": "x"}},
        )
    assert resp.status_code == 403, resp.text


def test_agent_runner_cannot_register_mcp_connection(mcp_app: FastAPI) -> None:
    _as(mcp_app, make_test_principal(role=UserRole.AGENT_RUNNER))
    from forge_mcp.testing import sample_connection

    conn = sample_connection()
    with TestClient(mcp_app) as client:
        resp = client.post("/mcp/connections", json=conn.model_dump(mode="json"))
    assert resp.status_code == 403, resp.text


def test_mcp_connections_are_workspace_scoped(mcp_app: FastAPI) -> None:
    # Workspace A registers a connection.
    _as(mcp_app, make_test_principal(role=UserRole.ADMIN, workspace_id=TEST_WORKSPACE_ID))
    with TestClient(mcp_app) as client:
        _register(client)
        assert [c["id"] for c in client.get("/mcp/connections").json()] == [
            "confluence-engineering"
        ]

        # Workspace B (also admin) must not see, read, call, or audit A's connection.
        _as(mcp_app, make_test_principal(role=UserRole.ADMIN, workspace_id=OTHER_WORKSPACE_ID))
        assert client.get("/mcp/connections").json() == []
        assert client.get("/mcp/connections/confluence-engineering/resources").status_code == 404
        assert (
            client.get(
                "/mcp/connections/confluence-engineering/resources/read",
                params={"uri": "confluence://engineering/page-1"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/mcp/connections/confluence-engineering/tools/call",
                json={"name": "search_pages", "arguments": {"q": "x"}},
            ).status_code
            == 404
        )
        assert client.get("/mcp/connections/confluence-engineering/audit").status_code == 404


# --------------------------------------------------------------------------- #
# 3. Observability audit + run traces are workspace-scoped                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def obs_app() -> Iterator[tuple[FastAPI, object]]:
    from forge_api.observability.service import ObservabilityService, get_observability_service

    app = create_app()
    service = ObservabilityService()
    app.dependency_overrides[get_observability_service] = lambda: service
    yield app, service


def test_audit_log_is_workspace_scoped(obs_app: tuple[FastAPI, object]) -> None:
    from forge_api.observability.audit import AuditCategory

    app, service = obs_app
    service.audit.record(  # type: ignore[attr-defined]
        category=AuditCategory.TOOL_CALL,
        action="tenant-a-secret-action",
        workspace_id=TEST_WORKSPACE_ID,
    )
    service.audit.record(  # type: ignore[attr-defined]
        category=AuditCategory.TOOL_CALL,
        action="tenant-b-action",
        workspace_id=OTHER_WORKSPACE_ID,
    )

    # Tenant B must not see tenant A's audit entry.
    _as(app, make_test_principal(workspace_id=OTHER_WORKSPACE_ID))
    with TestClient(app) as client:
        actions_b = {e["action"] for e in client.get("/observability/audit").json()}
    assert actions_b == {"tenant-b-action"}
    assert "tenant-a-secret-action" not in actions_b

    # Tenant A sees only its own.
    _as(app, make_test_principal(workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        actions_a = {e["action"] for e in client.get("/observability/audit").json()}
    assert actions_a == {"tenant-a-secret-action"}


def test_run_trace_is_workspace_scoped(obs_app: tuple[FastAPI, object]) -> None:
    from forge_contracts import Step
    from forge_contracts.enums import RunStatus, StepKind

    app, service = obs_app
    run_id = uuid.uuid4()
    service.record_run(  # type: ignore[attr-defined]
        run_id,
        steps=[Step(index=0, kind=StepKind.PLAN)],
        status=RunStatus.SUCCEEDED,
        workspace_id=TEST_WORKSPACE_ID,
    )

    # Tenant B cannot fetch tenant A's run trace by id.
    _as(app, make_test_principal(workspace_id=OTHER_WORKSPACE_ID))
    with TestClient(app) as client:
        assert client.get(f"/observability/runs/{run_id}/trace").status_code == 404

    # Tenant A can.
    _as(app, make_test_principal(workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        assert client.get(f"/observability/runs/{run_id}/trace").status_code == 200


# --------------------------------------------------------------------------- #
# 4. Knowledge write routes RBAC                                                #
# --------------------------------------------------------------------------- #


def test_viewer_cannot_index_or_sync_knowledge() -> None:
    app = create_app()
    _as(app, make_test_principal(role=UserRole.VIEWER))
    sid = str(uuid.uuid4())
    with TestClient(app) as client:
        index = client.post(
            "/knowledge/index",
            json={"source_id": sid, "chunks": [{"content": "x", "path": "x.py"}]},
        )
        sync = client.post(
            "/knowledge/sync",
            json={"source_id": sid, "mode": "full", "files": {"a.py": "def a(): ..."}},
        )
    assert index.status_code == 403, index.text
    assert sync.status_code == 403, sync.text
