"""Fixtures for the PM router integration tests.

In-memory SQLite (StaticPool, shared across the app's worker thread) + a
``FixturePMTransport`` factory so health/webhook verification run with zero
sockets. The PM service + auth principal are injected via dependency overrides.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.crypto import HmacAeadCipher
from forge_api.auth.vault import SecretVault
from forge_api.main import create_app
from forge_api.observability.audit import AuditLog
from forge_api.routers.pm import get_pm_service
from forge_api.services.pm_service import PMConnectionService
from forge_contracts.pm import HttpResponse, PMProvider
from forge_db.base import Base
from forge_db.models import Project, Workspace
from forge_db.models.pm import PMConnection
from forge_integrations.pm import FixturePMTransport

# Reuse the integration-sdk recorded fixtures.
SDK_FIXTURES = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "integration-sdk"
    / "tests"
    / "pm"
    / "fixtures"
)

WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
JIRA_API = "/rest/api/3"


def _load(rel: str) -> Any:
    return json.loads((SDK_FIXTURES / rel).read_text())


def _ok(body: Any) -> HttpResponse:
    return HttpResponse(status_code=200, json_body=body)


def _transport_factory(connection: PMConnection) -> FixturePMTransport:
    if connection.provider == PMProvider.jira:
        return FixturePMTransport(
            {
                ("GET", f"{JIRA_API}/issue/10001"): _ok(_load("jira/get_issue.json")),
                ("GET", f"{JIRA_API}/myself"): _ok(_load("jira/myself.json")),
            }
        )
    return FixturePMTransport(
        {
            ("POST", "Viewer"): _ok(_load("linear/viewer.json")),
            ("POST", "Issue"): _ok(_load("linear/issue_query.json")),
        }
    )


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        for ws_id, slug in ((WORKSPACE_ID, "acme"), (OTHER_WORKSPACE_ID, "other")):
            session.add(Workspace(id=ws_id, name=slug.title(), slug=slug))
            session.flush()
            session.add(
                Project(
                    id=uuid.uuid4(),
                    workspace_id=ws_id,
                    name=f"{slug} proj",
                    key=f"{slug.upper()}P",
                )
            )
        session.commit()
    return factory


@pytest.fixture
def vault() -> SecretVault:
    return SecretVault(cipher=HmacAeadCipher(b"0" * 32))


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def enqueued() -> list[uuid.UUID]:
    """Delivery row ids handed to the worker-enqueue seam (recorded, no broker)."""
    return []


@pytest.fixture
def pm_service(
    session_factory: sessionmaker[Session],
    vault: SecretVault,
    audit: AuditLog,
    enqueued: list[uuid.UUID],
) -> PMConnectionService:
    return PMConnectionService(
        session_factory=session_factory,
        vault=vault,
        audit=audit,
        transport_factory=_transport_factory,
        process_webhook_enqueue=enqueued.append,
    )


@pytest.fixture
def project_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        proj = session.query(Project).filter(Project.workspace_id == WORKSPACE_ID).first()
        return proj.id


@pytest.fixture
def client_factory(
    pm_service: PMConnectionService,
    authenticate_app: Callable[..., FastAPI],
) -> Iterator[Callable[..., TestClient]]:
    """Return a builder for an authenticated TestClient with a chosen role."""
    from forge_api.deps import Principal
    from forge_contracts import UserRole

    clients: list[TestClient] = []

    def _build(
        role: UserRole = UserRole.ADMIN, workspace_id: uuid.UUID = WORKSPACE_ID
    ) -> TestClient:
        app = create_app()
        principal = Principal(
            user_id=uuid.UUID("00000000-0000-0000-0000-0000000000b2"),
            workspace_id=workspace_id,
            role=role,
            email="pm-test@forge.local",
            auth_method="test",
            scopes=["*"],
        )
        authenticate_app(app, principal)
        app.dependency_overrides[get_pm_service] = lambda: pm_service
        tc = TestClient(app)
        clients.append(tc)
        return tc

    yield _build
    for tc in clients:
        tc.close()


@pytest.fixture
def client(client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory()
