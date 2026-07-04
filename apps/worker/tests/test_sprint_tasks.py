"""Integration tests for the F26 sprint worker tasks (snapshot/recompute/reconcile).

Hermetic: in-memory SQLite (StaticPool). Covers AC #8 (snapshot idempotency),
AC #14 (recompute idempotency), AC #18 (reconcile rebuild), and that the tasks
are registered on the Celery app + the daily Beat entry exists.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_board.sprint_service import SprintService
from forge_contracts.enums import SprintState, TaskStatus
from forge_db.base import Base
from forge_db.models import Project, Sprint, SprintBurndownSnapshot, SprintVelocity, Task, Workspace
from forge_worker.beat import BEAT_SCHEDULE, SPRINT_SNAPSHOT_TASK
from forge_worker.celery_app import celery_app
from forge_worker.tasks.sprint_tasks import (
    RECOMPUTE_TASK,
    RECONCILE_TASK,
    SNAPSHOT_TASK,
    recompute,
    reconcile,
    snapshot_active,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000d4")


@pytest.fixture
def sf() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.add(Workspace(id=WS, name="Acme", slug="acme"))
        session.flush()
        session.add(Project(id=PROJECT, workspace_id=WS, name="Core", key="CORE"))
        session.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def _active_sprint(sf: sessionmaker[Session]) -> uuid.UUID:
    with sf() as session:
        sprint = Sprint(
            workspace_id=WS, project_id=PROJECT, name="S1", status="planned",
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 14),
        )
        session.add(sprint)
        session.flush()
        session.add(Task(
            workspace_id=WS, project_id=PROJECT, key="CORE-1", title="t",
            status=TaskStatus.IN_PROGRESS, estimate=8, sprint_id=sprint.id,
        ))
        sid = sprint.id
        session.commit()
    SprintService(sf).start(workspace_id=WS, sprint_id=sid)
    return sid


def test_tasks_registered_and_beat_entry() -> None:
    for name in (SNAPSHOT_TASK, RECOMPUTE_TASK, RECONCILE_TASK):
        assert name in celery_app.tasks
    entry = BEAT_SCHEDULE["sprint-snapshot-burndown"]
    assert entry["task"] == SPRINT_SNAPSHOT_TASK


def test_snapshot_active_idempotent(sf: sessionmaker[Session]) -> None:
    _active_sprint(sf)
    assert snapshot_active(sf) == 1
    assert snapshot_active(sf) == 1  # re-run upserts; no new active count error
    with sf() as session:
        snaps = list(
            session.execute(
                select(SprintBurndownSnapshot).where(
                    SprintBurndownSnapshot.snapshot_date == date.today()
                )
            ).scalars()
        )
        assert len(snaps) == 1


def test_recompute_idempotent(sf: sessionmaker[Session]) -> None:
    sid = _active_sprint(sf)
    v1 = recompute(sf, sid)
    v2 = recompute(sf, sid)
    assert v2 > v1  # version bumps
    with sf() as session:
        row = session.execute(select(SprintVelocity)).scalar_one()
        assert row.committed_points == 8


def test_reconcile_rebuilds_from_log(sf: sessionmaker[Session]) -> None:
    sid = _active_sprint(sf)
    # Drop derived state, then reconcile rebuilds it from the event log.
    with sf() as session:
        for v in session.execute(select(SprintVelocity)).scalars():
            session.delete(v)
        for s in session.execute(select(SprintBurndownSnapshot)).scalars():
            session.delete(s)
        session.commit()
    reconcile(sf, sid)
    with sf() as session:
        assert session.execute(select(SprintVelocity)).scalar_one().committed_points == 8
        snaps = list(session.execute(select(SprintBurndownSnapshot)).scalars())
        assert len(snaps) == 14  # one per day across the inclusive window
        assert session.get(Sprint, sid).status == SprintState.ACTIVE.value
