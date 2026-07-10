"""F40-OBS-ANALYTICS: immutable per-run skill-profile snapshot."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts import SkillProfile
from forge_db.base import Base
from forge_db.models import AgentRun, Project, Workspace
from forge_obs.analytics.skill_snapshot import (
    SqlSkillProfileSnapshotRepository,
    build_directives_payload,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_build_directives_payload_serializes_sets_as_sorted_lists() -> None:
    profile = SkillProfile(
        name="incident-response",
        min_test_coverage=80,
        review_required=True,
        allowed_actions=["read_logs", "query_metrics"],
        forbidden_actions=["delete_data"],
    )
    payload = build_directives_payload(profile)
    assert payload["allowed_actions"] == ["query_metrics", "read_logs"]
    assert payload["forbidden_actions"] == ["delete_data"]
    assert payload["min_test_coverage"] == 80
    assert payload["review_required"] is True


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def seeded(factory) -> dict:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        project = Project(workspace_id=ws.id, name="Forge", key=f"FRG{uuid.uuid4().hex[:4]}")
        session.add(project)
        session.flush()
        run = AgentRun(workspace_id=ws.id, role="primary", skill_profile="incident-response")
        session.add(run)
        session.commit()
        return {"ws": ws.id, "run": run.id}


@pytest.mark.usefixtures("pg_engine")
def test_record_is_idempotent_per_agent_run(factory, seeded) -> None:
    repo = SqlSkillProfileSnapshotRepository(factory)
    profile = SkillProfile(name="incident-response", min_test_coverage=80)
    first = repo.record(workspace_id=seeded["ws"], agent_run_id=seeded["run"], profile=profile)
    replay = repo.record(workspace_id=seeded["ws"], agent_run_id=seeded["run"], profile=profile)
    assert first.id == replay.id

    fetched = repo.get(agent_run_id=seeded["run"])
    assert fetched is not None
    assert fetched.profile_name == "incident-response"
    assert fetched.min_test_coverage == 80


@pytest.mark.usefixtures("pg_engine")
def test_snapshot_row_is_immutable_on_postgres(factory, seeded) -> None:
    repo = SqlSkillProfileSnapshotRepository(factory)
    snapshot = repo.record(
        workspace_id=seeded["ws"],
        agent_run_id=seeded["run"],
        profile=SkillProfile(name="incident-response"),
    )

    with factory() as session, pytest.raises(DBAPIError, match="append-only"):
        session.execute(
            text("UPDATE skill_profile_snapshot SET profile_name = 'tampered' WHERE id = :id"),
            {"id": snapshot.id},
        )
        session.commit()
