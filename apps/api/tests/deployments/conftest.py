"""Fixtures for the deployments router integration tests (F31).

In-memory SQLite (StaticPool, shared across the app worker thread) seeded with a
workspace + project + users, with a ``DeploymentService`` wired to fake policy/CI
readers + Null providers/health and a fixed (non-frozen) clock, injected via
dependency override. Role-parametrized TestClient builder.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.observability.audit import AuditLog
from forge_api.routers.deployments import get_deployment_service
from forge_api.services.deployment_service import DeploymentService
from forge_contracts import UserRole
from forge_contracts.dtos import DeployRules
from forge_db.base import Base
from forge_db.models import Project, User, Workspace
from forge_deploy.freeze import FakeClock

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
APPROVER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b4")
APPROVER2_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b5")
REPO_ID = "github.com/org/api"

_ROLE_USER = {
    UserRole.ADMIN: ADMIN_ID,
    UserRole.MEMBER: MEMBER_ID,
    UserRole.VIEWER: VIEWER_ID,
}
_seeded: dict[str, uuid.UUID] = {}


class FakePolicy:
    def __init__(self, rules: DeployRules) -> None:
        self.rules = rules

    def deploy_rules(self, repo_id: str) -> DeployRules:
        return self.rules


class FakeGitHub:
    def __init__(self, status: str = "success") -> None:
        self.status = status

    def get_combined_status(self, repo_id: str, commit_sha: str) -> str | None:
        return self.status


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        s.add(Workspace(id=OTHER_WS_ID, name="Other", slug="other"))
        s.flush()
        for uid, email, role in (
            (ADMIN_ID, "admin@acme.test", "admin"),
            (MEMBER_ID, "member@acme.test", "member"),
            (VIEWER_ID, "viewer@acme.test", "viewer"),
            (APPROVER_ID, "approver@acme.test", "member"),
            (APPROVER2_ID, "approver2@acme.test", "member"),
        ):
            s.add(User(id=uid, workspace_id=WS_ID, email=email, name="U", role=role))
        project = Project(workspace_id=WS_ID, name="API", key="API")
        s.add(project)
        s.flush()
        _seeded["project"] = project.id
        s.commit()
    return factory


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 6, 24, 12, 0, tzinfo=UTC))  # Wed noon — open


@pytest.fixture
def policy_rules() -> DeployRules:
    return DeployRules(
        allow_agent_deploy=False,
        environments=["dev"],
        restricted_environments=["staging", "production"],
    )


@pytest.fixture
def service(
    session_factory: sessionmaker[Session],
    clock: FakeClock,
    policy_rules: DeployRules,
) -> DeploymentService:
    return DeploymentService(
        session_factory=session_factory,
        audit=AuditLog(),
        policy_reader=FakePolicy(policy_rules),
        ci_reader=FakeGitHub("success"),
        clock=clock,
    )


@pytest.fixture
def project_id() -> uuid.UUID:
    return _seeded["project"]


@pytest.fixture
def client_factory(
    service: DeploymentService,
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(
        role: UserRole = UserRole.MEMBER,
        *,
        workspace_id: uuid.UUID = WS_ID,
        user_id: uuid.UUID | None = None,
    ) -> TestClient:
        app = create_app()
        principal = Principal(
            user_id=user_id or _ROLE_USER.get(role, MEMBER_ID),
            workspace_id=workspace_id,
            role=role,
            email="deploy-test@forge.local",
            auth_method="test",
            scopes=["*"],
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_deployment_service] = lambda: service
        tc = TestClient(app)
        clients.append(tc)
        return tc

    yield _build
    for tc in clients:
        tc.close()


@pytest.fixture
def client(client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory(UserRole.MEMBER)


def pipeline_body(version: int = 1) -> dict:
    return {
        "repo_id": REPO_ID,
        "enabled": True,
        "version": version,
        "environments": [
            {
                "name": "dev",
                "rank": 0,
                "requires_approval": False,
                "gate_config": {"required_checks": ["ci_green"]},
                "provider_config": {"provider": "null"},
                "health_check": {"kind": "none"},
            },
            {
                "name": "staging",
                "rank": 1,
                "gate_config": {"required_checks": ["ci_green"], "min_approvals": 1},
                "provider_config": {"provider": "null"},
                "health_check": {"kind": "none"},
            },
            {
                "name": "production",
                "rank": 2,
                "gate_config": {
                    "required_checks": ["ci_green"],
                    "min_approvals": 2,
                    "auto_rollback": True,
                },
                "provider_config": {"provider": "null"},
                "health_check": {"kind": "none"},
            },
        ],
    }


def configure_pipeline(client: TestClient, project_id: uuid.UUID, version: int = 1):
    return client.put(f"/projects/{project_id}/pipeline", json=pipeline_body(version=version))
