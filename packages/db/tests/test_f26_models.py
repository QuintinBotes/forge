"""Postgres integration tests for the F26 sprint-velocity models.

Exercises the real Postgres code paths the SQLite unit tests cannot:

* the partial unique index ``uq_active_sprint_per_project`` (at most one
  ``active`` sprint per project; ``planned`` rows are unconstrained),
* the append-only immutability trigger on ``sprint_scope_event`` (BEFORE
  UPDATE/DELETE raises), and
* the ``(sprint_id, snapshot_date)`` uniqueness of ``sprint_burndown_snapshot``.

Uses the shared ``pg_engine`` fixture; parks without Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from forge_db.base import Base
from forge_db.models import (
    Project,
    Sprint,
    SprintBurndownSnapshot,
    SprintScopeEvent,
    Workspace,
)
from forge_db.models.enums import ScopeActorKind, SprintScopeEventType, SprintState

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _seed_project(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    return ws.id, project.id


def _sprint(ws_id: uuid.UUID, project_id: uuid.UUID, status: str) -> Sprint:
    return Sprint(workspace_id=ws_id, project_id=project_id, name=f"S-{uuid.uuid4().hex[:4]}",
                  status=status)


def test_one_active_sprint_per_project(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id = _seed_project(session)
        session.add(_sprint(ws_id, project_id, SprintState.ACTIVE))
        session.commit()
        # A second active sprint in the same project is rejected by the index.
        with pytest.raises(IntegrityError):
            session.add(_sprint(ws_id, project_id, SprintState.ACTIVE))
            session.commit()
        session.rollback()


def test_multiple_planned_sprints_allowed(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id = _seed_project(session)
        session.add_all(
            [
                _sprint(ws_id, project_id, SprintState.PLANNED),
                _sprint(ws_id, project_id, SprintState.PLANNED),
                _sprint(ws_id, project_id, SprintState.COMPLETED),
            ]
        )
        session.commit()  # no constraint on non-active states


def test_scope_event_is_append_only(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id = _seed_project(session)
        sprint = _sprint(ws_id, project_id, SprintState.ACTIVE)
        session.add(sprint)
        session.flush()
        event = SprintScopeEvent(
            workspace_id=ws_id,
            project_id=project_id,
            sprint_id=sprint.id,
            event_type=SprintScopeEventType.SPRINT_STARTED,
            points_delta=10,
            scope_points_after=10,
            remaining_points_after=10,
            actor_kind=ScopeActorKind.SYSTEM,
            occurred_at=datetime.now(UTC),
        )
        session.add(event)
        session.commit()

        # UPDATE is blocked by the immutability trigger.
        with pytest.raises((InternalError, ProgrammingError)):
            event.points_delta = 99
            session.commit()
        session.rollback()

        # DELETE is blocked too.
        with pytest.raises((InternalError, ProgrammingError)):
            session.delete(session.get(SprintScopeEvent, event.id))
            session.commit()
        session.rollback()


def test_burndown_snapshot_unique_per_day(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ws_id, project_id = _seed_project(session)
        sprint = _sprint(ws_id, project_id, SprintState.ACTIVE)
        session.add(sprint)
        session.flush()
        today = date(2026, 6, 26)

        def _snap() -> SprintBurndownSnapshot:
            return SprintBurndownSnapshot(
                workspace_id=ws_id,
                project_id=project_id,
                sprint_id=sprint.id,
                snapshot_date=today,
                scope_points=10,
                remaining_points=10,
                completed_points=0,
                ideal_points=10,
                completed_task_count=0,
                remaining_task_count=4,
            )

        session.add(_snap())
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(_snap())
            session.commit()
        session.rollback()
