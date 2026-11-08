"""Spec M persistence: GitHub Issues provider value, width, and defaults."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import Project, Workspace
from forge_db.models.enums import PMConflictPolicy, PMProvider
from forge_db.models.pm import PMConnection


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_pm_provider_has_github_issues() -> None:
    assert PMProvider.GITHUB_ISSUES.value == "github_issues"
    assert PMProvider("github_issues") is PMProvider.GITHUB_ISSUES


def test_pm_provider_columns_have_32_char_headroom() -> None:
    for table in ("pm_connection", "pm_task_link", "pm_webhook_delivery"):
        assert Base.metadata.tables[table].c.provider.type.length == 32, table


def test_new_connection_defaults_to_external_wins(factory: sessionmaker[Session]) -> None:
    ws_id = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=ws_id, name="Acme", slug="acme-pm"))
        session.flush()
        project = Project(workspace_id=ws_id, name="Core", key="COREPM")
        session.add(project)
        session.flush()
        conn = PMConnection(
            workspace_id=ws_id,
            provider=PMProvider.GITHUB_ISSUES,
            name="GitHub Issues",
            project_id=project.id,
            external_project_key="acme/widgets",
            external_project_id="10",
        )
        session.add(conn)
        session.commit()
        session.refresh(conn)
        # Spec M M2: the DB default for a new connection is tracker-wins.
        assert conn.conflict_policy is PMConflictPolicy.EXTERNAL_WINS
        assert conn.provider is PMProvider.GITHUB_ISSUES
