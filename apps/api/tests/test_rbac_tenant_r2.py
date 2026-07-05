"""Reproduction + regression tests for the Phase-2 round-2 security fixes (2.3-fix-r2).

Four real defects in the wired feature routers:

1. **RBAC never enforced** — a read-only ``viewer`` (and the ``agent-runner``
   identity) could perform writes / runs / approvals. Every write/run/approve
   route must now authorize, not just authenticate.
2. **HITL decider spoofing** — ``POST /approval/.../decision`` recorded
   ``decided_by`` from the request body, and any role could decide. The decider
   identity must come from the authenticated principal, and the ``agent-runner``
   must not be able to decide its own gate.
3. **Cross-workspace tenant isolation** — approval / agent / workflow / spec
   stores were process-wide and unscoped; workspace B could read/decide/overwrite
   workspace A's data. Foreign ids must surface as 404.
4. **sync_repo confused-deputy** — the route ignored ``connection_id`` and synced
   a caller-supplied repo with the server's privileged token. The connection must
   be resolved server-side, scoped to the caller's workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_contracts import RepositoryConnection, UserRole

# Deterministic identities (the tests dir is not an importable package, so these
# mirror conftest rather than importing it).
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
# A second, distinct workspace used for the tenant-isolation checks.
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
# 1. RBAC enforcement                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_with_agent() -> Iterator[FastAPI]:
    """App with an offline agent runner + fresh store wired (for run tests)."""
    from forge_api.routers.agent import (
        AgentRunStore,
        get_agent_runner,
        get_agent_store,
    )

    app = create_app()
    runner = AgentRunner(
        ScriptedModelClient(
            responses=[finish_response("done", confidence=0.95)],
            default=finish_response("done", confidence=0.95),
        )
    )
    store = AgentRunStore()
    app.dependency_overrides[get_agent_runner] = lambda: runner
    app.dependency_overrides[get_agent_store] = lambda: store
    yield app


def test_viewer_cannot_run_agent(app_with_agent: FastAPI) -> None:
    _as(app_with_agent, make_test_principal(role=UserRole.VIEWER))
    with TestClient(app_with_agent) as client:
        resp = client.post("/agent/runs", json={"objective": "do it"})
    assert resp.status_code == 403, resp.text


def test_agent_runner_can_run_agent(app_with_agent: FastAPI) -> None:
    _as(app_with_agent, make_test_principal(role=UserRole.AGENT_RUNNER))
    with TestClient(app_with_agent) as client:
        resp = client.post("/agent/runs", json={"objective": "do it"})
    assert resp.status_code == 201, resp.text


def test_viewer_cannot_write_or_run_across_routers() -> None:
    app = create_app()
    _as(app, make_test_principal(role=UserRole.VIEWER))
    rid = uuid.uuid4()
    with TestClient(app) as client:
        cases = [
            client.post(f"/approval/requests/{rid}/decision", json={"status": "approved"}),
            client.post(f"/spec/specs/{rid}/approve"),
            client.post(f"/workflow/runs/{rid}/transition", json={"event": "x"}),
            client.post(
                "/integration/github/pull-requests",
                json={"repo": "org/api", "title": "t", "head": "f", "base": "main"},
            ),
            client.post(
                "/approval/requests",
                json={"gate": "pr", "title": "t", "confidence": 0.5},
            ),
        ]
    for resp in cases:
        assert resp.status_code == 403, resp.text


def test_viewer_may_still_read() -> None:
    app = create_app()
    _as(app, make_test_principal(role=UserRole.VIEWER))
    with TestClient(app) as client:
        resp = client.get("/approval/requests")
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# 2. HITL decider identity                                                     #
# --------------------------------------------------------------------------- #


def _open_request(client: TestClient) -> str:
    resp = client.post(
        "/approval/requests",
        json={"gate": "pr", "title": "Approve PR", "confidence": 0.8},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_decider_identity_comes_from_principal_not_body() -> None:
    app = create_app()
    member = make_test_principal(role=UserRole.MEMBER)
    _as(app, member)
    with TestClient(app) as client:
        rid = _open_request(client)
        resp = client.post(
            f"/approval/requests/{rid}/decision",
            json={"status": "approved", "decided_by": "attacker", "reason": "ok"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The forged body identity is ignored; the authenticated principal is recorded.
    assert body["decided_by"] != "attacker"
    assert body["decided_by"] == member.email


def test_agent_runner_cannot_decide_its_own_gate() -> None:
    app = create_app()
    # Member opens the gate, then the agent-runner attempts to approve it.
    _as(app, make_test_principal(role=UserRole.MEMBER))
    with TestClient(app) as client:
        rid = _open_request(client)
        _as(app, make_test_principal(role=UserRole.AGENT_RUNNER))
        resp = client.post(f"/approval/requests/{rid}/decision", json={"status": "approved"})
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# 3. Cross-workspace tenant isolation                                          #
# --------------------------------------------------------------------------- #


def test_approval_is_workspace_scoped() -> None:
    app = create_app()
    _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        rid = _open_request(client)
        # Switch to a different tenant.
        _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=OTHER_WORKSPACE_ID))
        assert client.get(f"/approval/requests/{rid}").status_code == 404
        assert client.get("/approval/requests").json() == []
        decide = client.post(f"/approval/requests/{rid}/decision", json={"status": "approved"})
        assert decide.status_code == 404


def test_agent_run_is_workspace_scoped() -> None:
    from forge_api.routers.agent import (
        AgentRunStore,
        get_agent_runner,
        get_agent_store,
    )

    app = create_app()
    runner = AgentRunner(
        ScriptedModelClient(
            responses=[finish_response("done", confidence=0.95)],
            default=finish_response("done", confidence=0.95),
        )
    )
    store = AgentRunStore()
    app.dependency_overrides[get_agent_runner] = lambda: runner
    app.dependency_overrides[get_agent_store] = lambda: store

    _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        created = client.post("/agent/runs", json={"objective": "x"})
        run_id = created.json()["run_id"]
        _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=OTHER_WORKSPACE_ID))
        assert client.get(f"/agent/runs/{run_id}").status_code == 404


def test_workflow_run_is_workspace_scoped() -> None:
    from forge_api.routers.workflow import (
        WorkflowOwnership,
        get_workflow_engine,
        get_workflow_ownership,
    )
    from forge_workflow import WorkflowEngineImpl

    app = create_app()
    engine = WorkflowEngineImpl()
    ownership = WorkflowOwnership()
    app.dependency_overrides[get_workflow_engine] = lambda: engine
    app.dependency_overrides[get_workflow_ownership] = lambda: ownership

    _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        created = client.post("/workflow/runs", json={"task_id": str(uuid.uuid4())})
        run_id = created.json()["id"]
        _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=OTHER_WORKSPACE_ID))
        assert client.get(f"/workflow/runs/{run_id}").status_code == 404
        transition = client.post(
            f"/workflow/runs/{run_id}/transition", json={"event": "generate_spec_draft"}
        )
        assert transition.status_code == 404


def test_spec_is_workspace_scoped(tmp_path) -> None:
    from forge_api.routers.spec import SpecEngineRegistry, get_spec_registry
    from forge_spec import spec_id_for_key

    app = create_app()
    registry = SpecEngineRegistry(tmp_path / "specs")
    app.dependency_overrides[get_spec_registry] = lambda: registry

    _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        created = client.post(
            "/spec/specs",
            json={
                "epic_id": str(uuid.uuid4()),
                "name": "Customer search",
                "requirements": [{"id": "R1", "text": "search"}],
            },
        )
        assert created.status_code == 201, created.text
        spec_uuid = spec_id_for_key(created.json()["id"])
        # Same tenant can read its own spec.
        assert client.get(f"/spec/specs/{spec_uuid}").status_code == 200
        # A different tenant must not see it.
        _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=OTHER_WORKSPACE_ID))
        assert client.get(f"/spec/specs/{spec_uuid}").status_code == 404


# --------------------------------------------------------------------------- #
# 4. sync_repo confused-deputy                                                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sync_app() -> Iterator[tuple[FastAPI, list[str]]]:
    """App wired with a mock GitHub client recording the repos it is asked to sync."""
    from forge_api.routers.integration import (
        RepoConnectionStore,
        get_github_client,
        get_repo_connection_store,
    )
    from forge_integrations import GitHubClient

    synced: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # /repos/{owner}/{repo}/commits/{branch}
        parts = request.url.path.strip("/").split("/")
        if len(parts) >= 5 and parts[0] == "repos" and parts[3] == "commits":
            synced.append(f"{parts[1]}/{parts[2]}")
            return httpx.Response(200, json={"sha": "headsha"})
        return httpx.Response(404, json={"message": "nope"})

    gh = GitHubClient(token="server-byok", transport=httpx.MockTransport(handler))

    store = RepoConnectionStore()
    # ``last_synced_sha`` matching the mocked head short-circuits the sync after
    # the (server-resolved) head lookup, keeping the test focused on *which* repo
    # the server contacts rather than the full tree-walk.
    store.register(
        TEST_WORKSPACE_ID,
        RepositoryConnection(
            id=CONNECTION_ID,
            full_name="org/legit-repo",
            metadata={"last_synced_sha": "headsha"},
        ),
    )

    app = create_app()
    app.dependency_overrides[get_github_client] = lambda: gh
    app.dependency_overrides[get_repo_connection_store] = lambda: store
    yield app, synced


CONNECTION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


def test_sync_uses_server_connection_not_request_body(
    sync_app: tuple[FastAPI, list[str]],
) -> None:
    app, synced = sync_app
    _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        resp = client.post(
            f"/integration/github/repos/{CONNECTION_ID}/sync",
            # Attacker-supplied repo identity must be ignored.
            json={"full_name": "org/victim-repo"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["repo"] == "org/legit-repo"
    assert synced == ["org/legit-repo"]
    assert "org/victim-repo" not in synced


def test_sync_foreign_connection_is_404(
    sync_app: tuple[FastAPI, list[str]],
) -> None:
    app, synced = sync_app
    _as(app, make_test_principal(role=UserRole.MEMBER, workspace_id=OTHER_WORKSPACE_ID))
    with TestClient(app) as client:
        resp = client.post(f"/integration/github/repos/{CONNECTION_ID}/sync", json={})
    assert resp.status_code == 404, resp.text
    assert synced == []


def test_viewer_cannot_sync(sync_app: tuple[FastAPI, list[str]]) -> None:
    app, _synced = sync_app
    _as(app, make_test_principal(role=UserRole.VIEWER, workspace_id=TEST_WORKSPACE_ID))
    with TestClient(app) as client:
        resp = client.post(f"/integration/github/repos/{CONNECTION_ID}/sync", json={})
    assert resp.status_code == 403, resp.text
