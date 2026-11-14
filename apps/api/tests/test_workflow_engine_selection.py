"""F25 — workflow-engine selection + Temporal readiness (AC1, AC19)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge_api.main import app
from forge_api.services import temporal_health, workflow_engine
from forge_workflow import WorkflowEngineImpl
from forge_workflow.temporal.engine import TemporalWorkflowEngine


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_default_backend_is_postgres_fsm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_ENGINE_BACKEND", raising=False)
    assert workflow_engine.resolve_backend() == "postgres_fsm"
    engine = workflow_engine.select_workflow_engine()
    assert isinstance(engine, WorkflowEngineImpl)


def test_temporal_backend_selected_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    assert workflow_engine.resolve_backend() == "temporal"
    engine = workflow_engine.select_workflow_engine()
    assert isinstance(engine, TemporalWorkflowEngine)


def test_router_dependency_returns_selected_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid

    from sqlalchemy.orm import Session

    from forge_api.deps import Principal
    from forge_api.routers.workflow import get_workflow_engine
    from forge_contracts import UserRole
    from forge_workflow.store import SqlAlchemyWorkflowStore

    # The Temporal engine is built per request over the request's session (#169).
    session = Session()
    principal = Principal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role=UserRole.MEMBER,
        email="member@acme.test",
        auth_method="test",
        scopes=["*"],
    )
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    engine = get_workflow_engine(session, principal)
    assert isinstance(engine, TemporalWorkflowEngine)
    assert isinstance(engine.store, SqlAlchemyWorkflowStore)

    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "postgres_fsm")
    assert isinstance(get_workflow_engine(session, principal), WorkflowEngineImpl)


def test_readyz_fsm_backend_skips_temporal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "postgres_fsm")
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]["process"] == "ok"
    assert "temporal" not in body["checks"]


def test_readyz_temporal_unreachable_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    monkeypatch.setattr(temporal_health, "temporal_reachable", lambda settings=None: False)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["temporal"] == "unreachable"


def test_readyz_temporal_healthy_is_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    monkeypatch.setattr(temporal_health, "temporal_reachable", lambda settings=None: True)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"]["temporal"] == "ok"


# --------------------------------------------------------------------------- #
# F25 — forge-cli temporal bootstrap idempotency (AC20)                        #
# --------------------------------------------------------------------------- #


class _FakeWorkflowService:
    def __init__(self) -> None:
        self.registered: list[str] = []

    async def register_namespace(self, request) -> None:
        from temporalio.service import RPCError, RPCStatusCode

        if request.namespace in self.registered:
            raise RPCError("namespace already exists", RPCStatusCode.ALREADY_EXISTS, b"")
        self.registered.append(request.namespace)


class _FakeServiceClient:
    def __init__(self) -> None:
        self.workflow_service = _FakeWorkflowService()


class _FakeClient:
    def __init__(self) -> None:
        self.service_client = _FakeServiceClient()


async def test_temporal_bootstrap_is_idempotent() -> None:
    from forge_api.cli import bootstrap_namespace

    client = _FakeClient()
    created_first = await bootstrap_namespace(client, "forge", 30)
    created_second = await bootstrap_namespace(client, "forge", 30)

    assert created_first is True  # newly registered
    assert created_second is False  # idempotent re-run, no error
    assert client.service_client.workflow_service.registered == ["forge"]


def test_cli_parser_exposes_temporal_commands() -> None:
    from forge_api.cli import build_parser

    args = build_parser().parse_args(["temporal", "bootstrap"])
    assert (args.group, args.command) == ("temporal", "bootstrap")
    args = build_parser().parse_args(["temporal", "replay", "wf-123"])
    assert args.command == "replay" and args.workflow_id == "wf-123"
