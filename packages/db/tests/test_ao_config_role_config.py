"""Postgres integration tests for the ao-config ``agent_role_config`` model +
:class:`~forge_db.role_config.SqlRoleConfigStore`.

Exercises the real constraints: the partial unique index enforcing at most one
workspace-wide override per role, the plain unique constraint enforcing at most
one project-scoped override per (workspace, project, role), the upsert path
(insert then update-in-place), workspace CASCADE delete, and the resolver
(``forge_orchestration_policy.resolve_effective_config``) reading through the
real store end-to-end. Uses the shared ``pg_engine`` fixture; parks without
Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.orchestration_config import AgentRole, Effort
from forge_db.base import Base
from forge_db.models import AgentRoleConfig, Project, Workspace
from forge_db.role_config import SqlRoleConfigStore
from forge_orchestration_policy import resolve_effective_config

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Forge", key=f"F{uuid.uuid4().hex[:5]}")
    session.add(project)
    session.flush()
    return ws.id, project.id


def test_get_override_returns_none_when_absent(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed(session)
        store = SqlRoleConfigStore(session)
        assert store.get_override(ws_id, AgentRole.CODER) is None


def test_upsert_then_get_workspace_override(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed(session)
        store = SqlRoleConfigStore(session)
        created = store.upsert_override(ws_id, AgentRole.PLANNER, "senior", Effort.MAX)
        session.commit()

        assert created.workspace_id == ws_id
        assert created.project_id is None
        assert created.model_or_tier == "senior"
        assert created.effort == Effort.MAX

        fetched = store.get_override(ws_id, AgentRole.PLANNER)
        assert fetched == created


def test_upsert_is_idempotent_update_in_place(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed(session)
        store = SqlRoleConfigStore(session)
        store.upsert_override(ws_id, AgentRole.CODER, "medior", Effort.LOW)
        session.commit()

        store.upsert_override(ws_id, AgentRole.CODER, "senior", Effort.HIGH)
        session.commit()

        rows = session.scalars(
            select(AgentRoleConfig).where(
                AgentRoleConfig.workspace_id == ws_id, AgentRoleConfig.role == AgentRole.CODER
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].model_or_tier == "senior"
        assert rows[0].effort == Effort.HIGH


def test_project_override_independent_of_workspace_override(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        ws_id, project_id = _seed(session)
        store = SqlRoleConfigStore(session)
        store.upsert_override(ws_id, AgentRole.REVIEWER, "medior", Effort.MEDIUM)
        store.upsert_override(
            ws_id, AgentRole.REVIEWER, "claude-opus-4-6", Effort.MAX, project_id=project_id
        )
        session.commit()

        ws_row = store.get_override(ws_id, AgentRole.REVIEWER)
        assert ws_row is not None
        assert ws_row.project_id is None
        assert ws_row.model_or_tier == "medior"

        proj_row = store.get_override(ws_id, AgentRole.REVIEWER, project_id=project_id)
        assert proj_row is not None
        assert proj_row.project_id == project_id
        assert proj_row.model_or_tier == "claude-opus-4-6"


def test_workspace_default_partial_unique_index_rejects_duplicate_insert(
    factory: sessionmaker[Session],
) -> None:
    """Bypassing the repository's upsert with a raw duplicate INSERT must fail:
    proves the ``uq_agent_role_config_workspace_default`` partial unique index
    is real DB-level enforcement, not just application-level upsert discipline.
    """
    with factory() as session:
        ws_id, _ = _seed(session)
        session.add(
            AgentRoleConfig(
                workspace_id=ws_id,
                project_id=None,
                role=AgentRole.CODER,
                model_or_tier="medior",
                effort=Effort.MEDIUM,
            )
        )
        session.commit()

        session.add(
            AgentRoleConfig(
                workspace_id=ws_id,
                project_id=None,
                role=AgentRole.CODER,
                model_or_tier="senior",
                effort=Effort.HIGH,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_project_unique_constraint_rejects_duplicate_insert(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        ws_id, project_id = _seed(session)
        session.add(
            AgentRoleConfig(
                workspace_id=ws_id,
                project_id=project_id,
                role=AgentRole.CODER,
                model_or_tier="medior",
                effort=Effort.MEDIUM,
            )
        )
        session.commit()

        session.add(
            AgentRoleConfig(
                workspace_id=ws_id,
                project_id=project_id,
                role=AgentRole.CODER,
                model_or_tier="senior",
                effort=Effort.HIGH,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_delete_override_returns_false_when_absent(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed(session)
        store = SqlRoleConfigStore(session)
        assert store.delete_override(ws_id, AgentRole.COORDINATOR) is False


def test_delete_override_removes_row(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed(session)
        store = SqlRoleConfigStore(session)
        store.upsert_override(ws_id, AgentRole.COORDINATOR, "senior", Effort.HIGH)
        session.commit()

        assert store.delete_override(ws_id, AgentRole.COORDINATOR) is True
        session.commit()
        assert store.get_override(ws_id, AgentRole.COORDINATOR) is None


def test_list_overrides_scopes_by_project(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id = _seed(session)
        store = SqlRoleConfigStore(session)
        store.upsert_override(ws_id, AgentRole.PLANNER, "senior", Effort.HIGH)
        store.upsert_override(
            ws_id, AgentRole.CODER, "medior", Effort.MEDIUM, project_id=project_id
        )
        session.commit()

        workspace_only = store.list_overrides(ws_id)
        assert {row.role for row in workspace_only} == {AgentRole.PLANNER, AgentRole.CODER}

        project_only = store.list_overrides(ws_id, project_id=project_id)
        assert {row.role for row in project_only} == {AgentRole.CODER}


def test_workspace_cascade_delete_removes_role_config(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, _ = _seed(session)
        store = SqlRoleConfigStore(session)
        store.upsert_override(ws_id, AgentRole.PLANNER, "senior", Effort.HIGH)
        session.commit()

        session.execute(Workspace.__table__.delete().where(Workspace.id == ws_id))
        session.commit()

        remaining = session.scalars(
            select(AgentRoleConfig).where(AgentRoleConfig.workspace_id == ws_id)
        ).all()
        assert remaining == []


def test_resolver_reads_through_the_real_store(factory: sessionmaker[Session]) -> None:
    """End-to-end: ``forge_orchestration_policy.resolve_effective_config`` against
    the real ``SqlRoleConfigStore`` -- default, then workspace override, then
    project override taking precedence, exactly as the fake-store unit tests
    assert in ``packages/orchestration-policy/tests/test_role_config.py``.
    """
    with factory() as session:
        ws_id, project_id = _seed(session)
        store = SqlRoleConfigStore(session)

        default_resolved = resolve_effective_config(store, ws_id, AgentRole.SPEC_AUTHOR)
        assert default_resolved.source == "default"

        store.upsert_override(ws_id, AgentRole.SPEC_AUTHOR, "senior", Effort.HIGH)
        session.commit()
        ws_resolved = resolve_effective_config(store, ws_id, AgentRole.SPEC_AUTHOR)
        assert ws_resolved.source == "workspace"
        assert ws_resolved.model_or_tier == "senior"

        store.upsert_override(
            ws_id,
            AgentRole.SPEC_AUTHOR,
            "claude-opus-4-6",
            Effort.MAX,
            project_id=project_id,
        )
        session.commit()
        proj_resolved = resolve_effective_config(
            store, ws_id, AgentRole.SPEC_AUTHOR, project_id=project_id
        )
        assert proj_resolved.source == "project"
        assert proj_resolved.model_or_tier == "claude-opus-4-6"
