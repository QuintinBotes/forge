"""Fixtures for the F26 sprint router integration tests.

In-memory SQLite (StaticPool, shared across the app's worker thread) seeded with
a workspace + project + tasks, the :class:`SprintService` injected via dependency
override, and a role-parametrized TestClient builder (mirrors the F21 automations
test harness).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.sprints import get_sprint_service
from forge_board.sprint_service import SprintService
from forge_contracts import UserRole
from forge_contracts.enums import TaskStatus
from forge_db.base import Base
from forge_db.models import Project, Task, Workspace

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


@pytest.fixture
def seeded() -> dict[str, uuid.UUID]:
    return {}


@pytest.fixture
def session_factory(seeded: dict[str, uuid.UUID]) -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        for ws_id, slug in ((WS_ID, "acme"), (OTHER_WS_ID, "other")):
            session.add(Workspace(id=ws_id, name=slug.title(), slug=slug))
            session.flush()
        project = Project(id=PROJECT_ID, workspace_id=WS_ID, name="Core", key="CORE")
        session.add(project)
        session.flush()
        seeded["project"] = project.id
        for i, pts in enumerate((3, 5, 2)):
            t = Task(
                workspace_id=WS_ID,
                project_id=project.id,
                key=f"CORE-{i + 1}",
                title=f"task {i}",
                status=TaskStatus.IN_PROGRESS,
                estimate=pts,
            )
            session.add(t)
            session.flush()
            seeded[f"task{i}"] = t.id
        session.commit()
    return factory


@pytest.fixture
def service(session_factory: sessionmaker[Session]) -> SprintService:
    return SprintService(session_factory=session_factory)


@pytest.fixture
def client_factory(
    service: SprintService,
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(role: UserRole = UserRole.ADMIN, workspace_id: uuid.UUID = WS_ID) -> TestClient:
        app = create_app()
        principal = Principal(
            user_id=USER_ID,
            workspace_id=workspace_id,
            role=role,
            email="sprint-test@forge.local",
            auth_method="test",
            scopes=["*"],
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_sprint_service] = lambda: service
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
