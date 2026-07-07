"""Fixtures for the automations router integration tests (F21).

In-memory SQLite (StaticPool, shared across the app's worker thread) seeded with
a workspace + project + spec + tasks + a user, with the ``AutomationRuleService``
injected via dependency override and a role-parametrized TestClient builder.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.observability.audit import AuditLog
from forge_api.routers.automations import get_automation_service
from forge_api.services.automations import AutomationRuleService
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import Project, SpecDocument, Task, User, Workspace
from forge_db.models.enums import TaskStatus

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture
def seeded() -> dict[str, uuid.UUID]:
    return {}


@pytest.fixture
def session_factory(seeded: dict[str, uuid.UUID]) -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        for ws_id, slug in ((WS_ID, "acme"), (OTHER_WS_ID, "other")):
            session.add(Workspace(id=ws_id, name=slug.title(), slug=slug))
            session.flush()
        session.add(
            User(id=USER_ID, workspace_id=WS_ID, email="dev@acme.test", name="Dev", role="admin")
        )
        project = Project(workspace_id=WS_ID, name="Core", key="CORE")
        session.add(project)
        session.flush()
        spec = SpecDocument(workspace_id=WS_ID, project_id=project.id, spec_key="SPEC-17", name="S")
        session.add(spec)
        session.flush()
        seeded["project"] = project.id
        seeded["spec"] = spec.id
        statuses = [TaskStatus.IN_REVIEW, TaskStatus.IN_PROGRESS, TaskStatus.BACKLOG]
        for i, status in enumerate(statuses):
            t = Task(
                workspace_id=WS_ID,
                project_id=project.id,
                spec_id=spec.id,
                key=f"CORE-{i + 1}",
                title=f"task {i}",
                status=status,
            )
            session.add(t)
            session.flush()
            seeded[f"task{i}"] = t.id
        session.commit()
    return factory


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def service(session_factory: sessionmaker[Session], audit: AuditLog) -> AutomationRuleService:
    return AutomationRuleService(session_factory=session_factory, audit=audit)


@pytest.fixture
def client_factory(
    service: AutomationRuleService,
    authenticate_app: Callable[..., FastAPI],
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(role: UserRole = UserRole.ADMIN, workspace_id: uuid.UUID = WS_ID) -> TestClient:
        app = create_app()
        principal = Principal(
            user_id=USER_ID,
            workspace_id=workspace_id,
            role=role,
            email="auto-test@forge.local",
            auth_method="test",
            scopes=["*"],
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_automation_service] = lambda: service
        tc = TestClient(app)
        clients.append(tc)
        return tc

    yield _build
    for tc in clients:
        tc.close()


@pytest.fixture
def client(client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory()


@pytest.fixture
def project_id(seeded: dict[str, uuid.UUID], session_factory) -> uuid.UUID:
    return seeded["project"]


def close_spec_rule_body() -> dict:
    return {
        "name": "Close spec tasks on merge",
        "trigger": {"type": "workflow_state_changed", "config": {"to_state": "merged"}},
        "condition": {
            "match": "all",
            "conditions": [{"field": "has_spec", "op": "eq", "value": True}],
        },
        "actions": [
            {
                "type": "close_linked_spec_tasks",
                "scope": "project",
                "target_status": "done",
                "exclude_trigger_task": True,
            }
        ],
        "run_order": 100,
    }
