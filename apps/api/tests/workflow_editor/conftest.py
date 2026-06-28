"""Fixtures for the workflow-editor API tests (hermetic SQLite)."""

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
from forge_api.routers.workflow_editor import get_editor_service
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import User, Workspace
from forge_workflow.editor.catalog import RegistryCatalog
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository
from forge_workflow.editor.service import RecordingAuditSink, WorkflowEditorService

WS_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
WS_B = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        for ws in (WS_A, WS_B):
            session.add(Workspace(id=ws, name=f"ws-{ws.hex[:4]}", slug=f"ws-{ws.hex}"))
        session.flush()
        session.add(
            User(id=USER_ID, workspace_id=WS_A, email="admin@forge.test", name="Admin")
        )
        session.commit()
    return factory


@pytest.fixture
def audit() -> RecordingAuditSink:
    return RecordingAuditSink()


def _principal(role: UserRole, workspace_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=USER_ID,
        workspace_id=workspace_id,
        role=role,
        email="admin@forge.test",
        auth_method="test",
        scopes=["*"],
    )


@pytest.fixture
def make_client(
    session_factory: sessionmaker[Session], audit: RecordingAuditSink
) -> Callable[..., TestClient]:
    catalog = RegistryCatalog()

    def _build(
        role: UserRole = UserRole.ADMIN,
        workspace_id: uuid.UUID = WS_A,
        authed: bool = True,
    ) -> TestClient:
        app: FastAPI = create_app()

        def _service() -> WorkflowEditorService:
            return WorkflowEditorService(
                DbWorkflowDefinitionRepository(session_factory()),
                catalog=catalog,
                audit=audit,
            )

        app.dependency_overrides[get_editor_service] = _service
        if authed:
            app.dependency_overrides[get_current_principal] = lambda: _principal(
                role, workspace_id
            )
        return TestClient(app)

    return _build


@pytest.fixture
def admin_client(make_client: Callable[..., TestClient]) -> Iterator[TestClient]:
    with make_client() as client:
        yield client
