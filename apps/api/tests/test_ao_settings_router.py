"""Integration tests for the Adaptive Orchestration settings API (``/ao/*``).

Real handlers over a real Postgres session (``pg_engine``, shared root
fixture): per-role model+effort config (list/upsert/delete, workspace vs
project override precedence, RBAC, workspace isolation), the workspace-wide
settings (auto-route, tier-model overrides, complexity thresholds, RBAC), the
routing-preview endpoint (default sizing, custom thresholds, custom
tier-model overrides, invalid provider), and the Self-Eval Gate surface
(status read + the admin run trigger that enqueues ``forge.self_eval.run``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.routers import ao_settings as ao_module
from forge_api.services import self_eval_service as self_eval_service_module
from forge_api.settings import Settings
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import AuditLog, Project, User, Workspace
from forge_db.models.benchmark import BenchmarkSuite, SelfEvalBaseline

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _principal(role: UserRole, workspace_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role=role,
        email="test@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def _seed_workspace(factory: sessionmaker[Session], *, name: str, slug: str) -> uuid.UUID:
    with factory() as session:
        ws = Workspace(name=name, slug=slug)
        session.add(ws)
        session.flush()
        workspace_id = ws.id
        session.commit()
    return workspace_id


@pytest.fixture
def client_for(
    factory: sessionmaker[Session], authenticate_app: Callable[..., FastAPI]
) -> Callable[[UserRole], TestClient]:
    workspace_id = _seed_workspace(factory, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")

    def _make(role: UserRole) -> TestClient:
        app = create_app()
        authenticate_app(app, _principal(role, workspace_id))

        def _get_db() -> Iterator[Session]:
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _get_db
        return TestClient(app)

    return _make


# --------------------------------------------------------------------------- #
# Per-role model+effort config                                                #
# --------------------------------------------------------------------------- #


def test_list_role_config_returns_defaults_for_every_role(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.get("/ao/role-config")
    assert resp.status_code == 200
    items = resp.json()["items"]
    roles = {item["role"] for item in items}
    assert roles == {"planner", "coder", "reviewer", "spec_author", "coordinator"}
    assert all(item["source"] == "default" for item in items)


def test_viewer_cannot_upsert_role_config(client_for: Callable[[UserRole], TestClient]) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.put("/ao/role-config/coder", json={"model_or_tier": "senior", "effort": "max"})
    assert resp.status_code == 403


def test_admin_upsert_then_list_reflects_workspace_override(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.ADMIN)
    resp = client.put("/ao/role-config/coder", json={"model_or_tier": "senior", "effort": "max"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "workspace"

    listed = client.get("/ao/role-config").json()["items"]
    coder = next(item for item in listed if item["role"] == "coder")
    assert coder["model_or_tier"] == "senior"
    assert coder["effort"] == "max"
    assert coder["source"] == "workspace"


def test_project_override_beats_workspace_override(
    client_for: Callable[[UserRole], TestClient], factory: sessionmaker[Session]
) -> None:
    client = client_for(UserRole.ADMIN)

    # The client's workspace_id isn't exposed to the test directly; read it
    # back from the settings response so the seeded project scopes correctly.
    settings_resp = client.get("/ao/settings")
    workspace_id = uuid.UUID(settings_resp.json()["workspace_id"])

    with factory() as session:
        project = Project(workspace_id=workspace_id, name="Proj", key="PRJ1")
        session.add(project)
        session.flush()
        project_id = project.id
        session.commit()

    # Workspace-wide override for reviewer.
    resp = client.put(
        "/ao/role-config/reviewer", json={"model_or_tier": "senior", "effort": "medium"}
    )
    assert resp.status_code == 200

    # Project-scoped override takes precedence.
    resp = client.put(
        f"/ao/role-config/reviewer?project_id={project_id}",
        json={"model_or_tier": "claude-opus-4-8", "effort": "max"},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "project"

    listed = client.get(f"/ao/role-config?project_id={project_id}").json()["items"]
    reviewer = next(item for item in listed if item["role"] == "reviewer")
    assert reviewer["model_or_tier"] == "claude-opus-4-8"
    assert reviewer["source"] == "project"

    # Without project_id, the workspace-wide override is still in effect.
    listed_ws = client.get("/ao/role-config").json()["items"]
    reviewer_ws = next(item for item in listed_ws if item["role"] == "reviewer")
    assert reviewer_ws["model_or_tier"] == "senior"
    assert reviewer_ws["source"] == "workspace"


def test_admin_delete_reverts_to_default(client_for: Callable[[UserRole], TestClient]) -> None:
    client = client_for(UserRole.ADMIN)
    resp = client.put("/ao/role-config/planner", json={"model_or_tier": "senior", "effort": "max"})
    assert resp.status_code == 200

    resp = client.delete("/ao/role-config/planner")
    assert resp.status_code == 200
    assert resp.json()["source"] == "default"


def test_role_config_is_workspace_isolated(
    factory: sessionmaker[Session], authenticate_app: Callable[..., FastAPI]
) -> None:
    ws_a = _seed_workspace(factory, name="A", slug=f"a-{uuid.uuid4().hex[:8]}")
    ws_b = _seed_workspace(factory, name="B", slug=f"b-{uuid.uuid4().hex[:8]}")

    def _client(workspace_id: uuid.UUID) -> TestClient:
        app = create_app()
        authenticate_app(app, _principal(UserRole.ADMIN, workspace_id))

        def _get_db() -> Iterator[Session]:
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _get_db
        return TestClient(app)

    client_a = _client(ws_a)
    client_b = _client(ws_b)

    resp = client_a.put("/ao/role-config/coder", json={"model_or_tier": "senior", "effort": "max"})
    assert resp.status_code == 200

    listed_b = client_b.get("/ao/role-config").json()["items"]
    coder_b = next(item for item in listed_b if item["role"] == "coder")
    assert coder_b["source"] == "default"


# --------------------------------------------------------------------------- #
# Workspace-wide settings                                                     #
# --------------------------------------------------------------------------- #


def test_get_settings_defaults_when_unset(client_for: Callable[[UserRole], TestClient]) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.get("/ao/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_route"] is True
    assert body["tier_model_overrides"] == {}
    assert body["junior_max_is_default"] is True
    assert body["medior_max_is_default"] is True
    assert isinstance(body["junior_max"], int)
    assert isinstance(body["medior_max"], int)


def test_viewer_cannot_update_settings(client_for: Callable[[UserRole], TestClient]) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.put("/ao/settings", json={"auto_route": False})
    assert resp.status_code == 403


def test_admin_updates_settings_and_get_reflects_it(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.ADMIN)
    resp = client.put(
        "/ao/settings",
        json={
            "auto_route": False,
            "tier_model_overrides": {"anthropic": {"junior": "claude-haiku-4-5"}},
            "junior_max": 4,
            "medior_max": 12,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_route"] is False
    assert body["tier_model_overrides"] == {"anthropic": {"junior": "claude-haiku-4-5"}}
    assert body["junior_max"] == 4
    assert body["medior_max"] == 12
    assert body["junior_max_is_default"] is False

    fetched = client.get("/ao/settings").json()
    assert fetched["junior_max"] == 4
    assert fetched["auto_route"] is False


def test_clear_junior_max_resets_to_default(client_for: Callable[[UserRole], TestClient]) -> None:
    client = client_for(UserRole.ADMIN)
    client.put("/ao/settings", json={"junior_max": 4})
    resp = client.put("/ao/settings", json={"clear_junior_max": True})
    assert resp.status_code == 200
    assert resp.json()["junior_max_is_default"] is True


# --------------------------------------------------------------------------- #
# Routing preview                                                             #
# --------------------------------------------------------------------------- #


def test_routing_preview_trivial_task_is_junior_single(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.post(
        "/ao/routing-preview", json={"kind": "doc", "priority": "low", "file_count": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "junior"
    assert body["strategy"] == "single"
    assert body["model"]
    assert body["provider"] == "anthropic"
    assert "auto_route_enabled" in body
    assert body["reasons"]


def test_routing_preview_senior_swarm_task(client_for: Callable[[UserRole], TestClient]) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.post(
        "/ao/routing-preview",
        json={
            "kind": "change_request",
            "priority": "urgent",
            "blast_radius": "high",
            "file_count": 40,
            "requirement_count": 20,
            "acceptance_criteria_count": 20,
            "touches_contracts": True,
            "touches_security": True,
            "dependency_count": 10,
            "open_questions_count": 5,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "senior"
    assert body["strategy"] == "swarm"


def test_routing_preview_respects_workspace_thresholds(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.ADMIN)
    client.put("/ao/settings", json={"junior_max": 50})
    resp = client.post("/ao/routing-preview", json={"kind": "feature", "priority": "medium"})
    assert resp.status_code == 200
    assert resp.json()["tier"] == "junior"


def test_routing_preview_respects_tier_model_overrides(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.ADMIN)
    client.put(
        "/ao/settings",
        json={"tier_model_overrides": {"anthropic": {"junior": "custom-junior-model"}}},
    )
    resp = client.post(
        "/ao/routing-preview", json={"kind": "doc", "priority": "low", "file_count": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "custom-junior-model"


def test_routing_preview_rejects_invalid_provider(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.post(
        "/ao/routing-preview",
        json={"kind": "doc", "priority": "low", "provider": "not-a-provider"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Self-Eval Gate: status read + run trigger                                   #
# --------------------------------------------------------------------------- #


def _workspace_of(client: TestClient) -> uuid.UUID:
    return uuid.UUID(client.get("/ao/settings").json()["workspace_id"])


def _seed_private_suite(
    factory: sessionmaker[Session], workspace_id: uuid.UUID, *, published: bool = True
) -> uuid.UUID:
    with factory() as session:
        suite = BenchmarkSuite(
            slug=f"self-eval-{uuid.uuid4().hex[:8]}",
            version="1.0.0",
            title="Private self-eval suite",
            task_count=10,
            content_hash="deadbeef",
            frozen=True,
            published=published,
            workspace_id=workspace_id,
            repo_id="github:acme/app",
            private=True,
        )
        session.add(suite)
        session.flush()
        suite_id = suite.id
        session.commit()
    return suite_id


def _seed_baseline(
    factory: sessionmaker[Session], workspace_id: uuid.UUID, suite_id: uuid.UUID
) -> None:
    with factory() as session:
        session.add(
            SelfEvalBaseline(
                workspace_id=workspace_id,
                benchmark_suite_id=suite_id,
                baseline_rate=0.8,
                resolved=8,
                total=10,
                config={"scope": "ao.settings"},
            )
        )
        session.commit()


def test_self_eval_status_cold_start_is_honest_nulls(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.VIEWER)
    resp = client.get("/ao/self-eval/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enforced"] is False
    assert body["suite"] is None
    assert body["baseline"] is None


def test_self_eval_status_reports_suite_and_baseline(
    client_for: Callable[[UserRole], TestClient], factory: sessionmaker[Session]
) -> None:
    client = client_for(UserRole.VIEWER)
    workspace_id = _workspace_of(client)
    suite_id = _seed_private_suite(factory, workspace_id)
    _seed_baseline(factory, workspace_id, suite_id)

    body = client.get("/ao/self-eval/status").json()
    assert body["suite"]["id"] == str(suite_id)
    assert body["suite"]["published"] is True
    assert body["suite"]["repo_id"] == "github:acme/app"
    assert body["baseline"]["benchmark_suite_id"] == str(suite_id)
    assert body["baseline"]["baseline_rate"] == 0.8
    assert (body["baseline"]["resolved"], body["baseline"]["total"]) == (8, 10)
    assert body["baseline"]["recorded_at"]


def test_self_eval_status_reflects_enforcement_flag(
    client_for: Callable[[UserRole], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ao_module, "get_app_settings", lambda: Settings(self_eval_enforce=True))
    client = client_for(UserRole.VIEWER)
    assert client.get("/ao/self-eval/status").json()["enforced"] is True


def test_self_eval_status_is_workspace_isolated(
    factory: sessionmaker[Session], authenticate_app: Callable[..., FastAPI]
) -> None:
    ws_a = _seed_workspace(factory, name="A", slug=f"a-{uuid.uuid4().hex[:8]}")
    ws_b = _seed_workspace(factory, name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    suite_a = _seed_private_suite(factory, ws_a)
    _seed_baseline(factory, ws_a, suite_a)

    app = create_app()
    authenticate_app(app, _principal(UserRole.VIEWER, ws_b))

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    body = TestClient(app).get("/ao/self-eval/status").json()
    assert body["suite"] is None
    assert body["baseline"] is None


def test_viewer_cannot_request_self_eval_run(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.VIEWER)
    assert client.post("/ao/self-eval/runs").status_code == 403


def test_self_eval_run_without_private_suite_is_409(
    client_for: Callable[[UserRole], TestClient],
) -> None:
    client = client_for(UserRole.ADMIN)
    resp = client.post("/ao/self-eval/runs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_private_suite"


def test_self_eval_run_unpublished_suite_is_409(
    client_for: Callable[[UserRole], TestClient], factory: sessionmaker[Session]
) -> None:
    client = client_for(UserRole.ADMIN)
    _seed_private_suite(factory, _workspace_of(client), published=False)
    resp = client.post("/ao/self-eval/runs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_private_suite"


def test_admin_self_eval_run_enqueues_worker_task_and_audits(
    factory: sessionmaker[Session],
    authenticate_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Self-contained client: the run-request audit row carries a real actor FK,
    # so the principal must be a persisted user (unlike the shared client_for).
    workspace_id = _seed_workspace(factory, name="Run", slug=f"run-{uuid.uuid4().hex[:8]}")
    with factory() as session:
        admin = User(workspace_id=workspace_id, email="admin@forge.local", role=UserRole.ADMIN)
        session.add(admin)
        session.flush()
        user_id = admin.id
        session.commit()

    app = create_app()
    principal = Principal(
        user_id=user_id,
        workspace_id=workspace_id,
        role=UserRole.ADMIN,
        email="admin@forge.local",
        auth_method="test",
        scopes=["*"],
    )
    authenticate_app(app, principal)

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    client = TestClient(app)
    suite_id = _seed_private_suite(factory, workspace_id)

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        self_eval_service_module,
        "enqueue_self_eval_run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    resp = client.post("/ao/self-eval/runs")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["task"] == "forge.self_eval.run"
    assert body["benchmark_suite_id"] == str(suite_id)

    assert len(calls) == 1
    (ws_arg, config_arg), kwargs = calls[0]
    assert ws_arg == workspace_id
    assert config_arg["scope"] == "ao.settings"
    assert "auto_route" in config_arg
    assert kwargs["recorded_by"] is not None

    with factory() as session:
        actions = list(
            session.scalars(
                select(AuditLog.action).where(AuditLog.workspace_id == workspace_id)
            ).all()
        )
    assert "ao.self_eval.run_requested" in actions
