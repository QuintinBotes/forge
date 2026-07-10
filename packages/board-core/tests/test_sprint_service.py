"""Integration tests for the F26 :class:`SprintService` (SQLite).

Covers committed-scope snapshot (AC #1), one-active-sprint (AC #2), scope capture
(AC #3-5), complete + carryover (AC #7), burndown idempotency (AC #8), recompute
idempotency (AC #14), cancel (AC #15), and drop-and-reconcile (AC #18).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_board.exceptions import ActiveSprintExistsError
from forge_board.sprint_service import SprintService
from forge_contracts.automation import (
    AutomationEntityType,
    AutomationTriggerEnvelope,
    AutomationTriggerType,
)
from forge_contracts.enums import CarryoverTarget, SprintScopeEventType, SprintState, TaskStatus
from forge_db.base import Base
from forge_db.models import (
    Project,
    Sprint,
    SprintBurndownSnapshot,
    SprintScopeEvent,
    SprintVelocity,
    Task,
    Workspace,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


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
        session.add(Project(id=_PROJECT, workspace_id=WS, name="Core", key="CORE"))
        session.commit()
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


_PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000d4")
_counter = {"n": 0}


def _make_task(session: Session, *, points: int | None, status: TaskStatus, sprint_id=None) -> Task:
    _counter["n"] += 1
    task = Task(
        workspace_id=WS,
        project_id=_PROJECT,
        key=f"CORE-{_counter['n']}",
        title=f"task {_counter['n']}",
        status=status,
        estimate=points,
        sprint_id=sprint_id,
    )
    session.add(task)
    session.flush()
    return task


def _make_sprint(session: Session, *, status=SprintState.PLANNED) -> Sprint:
    sprint = Sprint(
        workspace_id=WS,
        project_id=_PROJECT,
        name=f"Sprint {uuid.uuid4().hex[:4]}",
        status=status.value,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 14),
    )
    session.add(sprint)
    session.flush()
    return sprint


def test_start_snapshots_committed_scope(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        for pts, st in [
            (3, TaskStatus.BACKLOG),
            (5, TaskStatus.IN_PROGRESS),
            (2, TaskStatus.BACKLOG),
            (0, TaskStatus.BACKLOG),
            (8, TaskStatus.DONE),
        ]:
            _make_task(session, points=pts, status=st, sprint_id=sprint.id)
        sid = sprint.id
        session.commit()

    svc = SprintService(sf)
    view = svc.start(workspace_id=WS, sprint_id=sid)
    assert view.state == SprintState.ACTIVE
    assert view.committed_points == 10  # 3+5+2+0, the DONE 8 excluded
    assert view.committed_task_count == 4
    assert view.started_at is not None

    with sf() as session:
        events = list(session.execute(select(SprintScopeEvent)).scalars())
        assert len(events) == 1
        assert events[0].event_type == SprintScopeEventType.SPRINT_STARTED
        assert events[0].scope_points_after == 10
        assert events[0].remaining_points_after == 10
        snaps = list(session.execute(select(SprintBurndownSnapshot)).scalars())
        assert len(snaps) == 1
        assert snaps[0].remaining_points == 10


def test_one_active_sprint_per_project(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        a = _make_sprint(session)
        b = _make_sprint(session)
        aid, bid = a.id, b.id
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=aid)
    with pytest.raises(ActiveSprintExistsError):
        svc.start(workspace_id=WS, sprint_id=bid)


def test_add_remove_capture(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        task = _make_task(session, points=5, status=TaskStatus.BACKLOG)
        tid = task.id
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)

    svc.add_task(workspace_id=WS, sprint_id=sid, task_id=tid)
    with sf() as session:
        added = session.execute(
            select(SprintScopeEvent).where(
                SprintScopeEvent.event_type == SprintScopeEventType.TASK_ADDED
            )
        ).scalar_one()
        assert added.points_delta == 5
        assert added.scope_points_after == 5
        v = session.execute(select(SprintVelocity)).scalar_one()
        assert v.added_points == 5

    svc.remove_task(workspace_id=WS, task_id=tid)
    with sf() as session:
        removed = session.execute(
            select(SprintScopeEvent).where(
                SprintScopeEvent.event_type == SprintScopeEventType.TASK_REMOVED
            )
        ).scalar_one()
        assert removed.points_delta == -5
        v = session.execute(select(SprintVelocity)).scalar_one()
        assert v.removed_points == 5


def test_complete_reopen_capture(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        task = _make_task(session, points=3, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
        tid = task.id
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)

    svc.set_task_status(workspace_id=WS, task_id=tid, status=TaskStatus.DONE)
    with sf() as session:
        v = session.execute(select(SprintVelocity)).scalar_one()
        assert v.completed_points == 3
        ev = session.execute(
            select(SprintScopeEvent).where(
                SprintScopeEvent.event_type == SprintScopeEventType.TASK_COMPLETED
            )
        ).scalar_one()
        assert ev.remaining_points_after == 0

    svc.set_task_status(workspace_id=WS, task_id=tid, status=TaskStatus.IN_PROGRESS)
    with sf() as session:
        v = session.execute(select(SprintVelocity)).scalar_one()
        assert v.completed_points == 0
        assert (
            session.execute(
                select(SprintScopeEvent).where(
                    SprintScopeEvent.event_type == SprintScopeEventType.TASK_REOPENED
                )
            ).scalar_one()
            is not None
        )


def test_estimate_change_capture(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        task = _make_task(session, points=2, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
        tid = task.id
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)
    svc.set_task_estimate(workspace_id=WS, task_id=tid, estimate=5)
    with sf() as session:
        ev = session.execute(
            select(SprintScopeEvent).where(
                SprintScopeEvent.event_type == SprintScopeEventType.ESTIMATE_CHANGED
            )
        ).scalar_one()
        assert ev.points_before == 2
        assert ev.points_after == 5
        assert ev.points_delta == 3
        assert ev.scope_points_after == 5  # committed 2 + 3
        assert ev.remaining_points_after == 5


def test_no_event_for_planned_sprint(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)  # planned, not started
        sid = sprint.id
        task = _make_task(session, points=5, status=TaskStatus.BACKLOG)
        tid = task.id
        session.commit()
    svc = SprintService(sf)
    svc.add_task(workspace_id=WS, sprint_id=sid, task_id=tid)
    with sf() as session:
        assert list(session.execute(select(SprintScopeEvent)).scalars()) == []


def test_complete_with_carryover_to_backlog(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        done = [
            _make_task(session, points=p, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
            for p in (10, 10, 5)
        ]
        carry = [
            _make_task(session, points=p, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
            for p in (4, 5)
        ]
        done_ids = [t.id for t in done]
        carry_ids = [t.id for t in carry]
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)
    for tid in done_ids:
        svc.set_task_status(workspace_id=WS, task_id=tid, status=TaskStatus.DONE)

    report = svc.complete(workspace_id=WS, sprint_id=sid, carryover=CarryoverTarget.BACKLOG)
    assert report.sprint.state == SprintState.COMPLETED
    assert report.velocity.committed_points == 34
    assert report.velocity.completed_points == 25
    assert report.velocity.carryover_points == 9
    assert report.velocity.carryover_task_count == 2
    # report buckets reconcile with rollup counters (AC #12)
    assert sum(t.points for t in report.completed) == 25
    assert sum(t.points for t in report.carryover) == 9

    with sf() as session:
        for tid in carry_ids:
            assert session.get(Task, tid).sprint_id is None  # moved to backlog
        assert (
            session.execute(
                select(SprintScopeEvent).where(
                    SprintScopeEvent.event_type == SprintScopeEventType.SPRINT_COMPLETED
                )
            ).scalar_one()
            is not None
        )


def test_complete_next_sprint_requires_id(sf: sessionmaker[Session]) -> None:
    from forge_board.sprint_service import InvalidSprintRequest

    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)
    with pytest.raises(InvalidSprintRequest):
        svc.complete(workspace_id=WS, sprint_id=sid, carryover=CarryoverTarget.NEXT_SPRINT)


def test_cancel_returns_tasks_and_excludes_from_history(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        task = _make_task(session, points=5, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
        tid = task.id
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)
    view = svc.cancel(workspace_id=WS, sprint_id=sid)
    assert view.state == SprintState.CANCELLED
    with sf() as session:
        assert session.get(Task, tid).sprint_id is None
        assert (
            session.execute(
                select(SprintScopeEvent).where(
                    SprintScopeEvent.event_type == SprintScopeEventType.SPRINT_CANCELLED
                )
            ).scalar_one()
            is not None
        )
    dash = svc.velocity_dashboard(workspace_id=WS, project_id=_PROJECT)
    assert all(b.sprint_id != sid for b in dash.sprints)


def test_burndown_snapshot_idempotent(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        _make_task(session, points=5, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)
    svc.snapshot_burndown_for_active(snapshot_date=date(2026, 6, 3))
    svc.snapshot_burndown_for_active(snapshot_date=date(2026, 6, 3))
    with sf() as session:
        snaps = list(
            session.execute(
                select(SprintBurndownSnapshot).where(
                    SprintBurndownSnapshot.snapshot_date == date(2026, 6, 3)
                )
            ).scalars()
        )
        assert len(snaps) == 1


def test_recompute_idempotent(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        _make_task(session, points=5, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)
    svc.recompute(workspace_id=WS, sprint_id=sid)
    with sf() as session:
        v1 = session.execute(select(SprintVelocity)).scalar_one()
        snap1 = (
            v1.committed_points,
            v1.completed_points,
            v1.carryover_points,
            v1.added_points,
            v1.removed_points,
        )
        version1 = session.get(Sprint, sid).velocity_version
    svc.recompute(workspace_id=WS, sprint_id=sid)
    with sf() as session:
        v2 = session.execute(select(SprintVelocity)).scalar_one()
        snap2 = (
            v2.committed_points,
            v2.completed_points,
            v2.carryover_points,
            v2.added_points,
            v2.removed_points,
        )
        version2 = session.get(Sprint, sid).velocity_version
    assert snap1 == snap2
    assert version2 > version1


def test_reconcile_byte_identical(sf: sessionmaker[Session]) -> None:
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        t1 = _make_task(session, points=8, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
        _make_task(session, points=5, status=TaskStatus.IN_PROGRESS, sprint_id=sprint.id)
        t1id = t1.id
        session.commit()
    svc = SprintService(sf)
    svc.start(workspace_id=WS, sprint_id=sid)
    svc.set_task_status(workspace_id=WS, task_id=t1id, status=TaskStatus.DONE)
    svc.snapshot_burndown_for_active(snapshot_date=date(2026, 6, 5))

    with sf() as session:
        v = session.execute(select(SprintVelocity)).scalar_one()
        before_rollup = (
            v.committed_points,
            v.completed_points,
            v.carryover_points,
            v.added_points,
            v.removed_points,
            v.completed_task_count,
            v.carryover_task_count,
        )
        before_snaps = {
            s.snapshot_date: (
                s.scope_points,
                s.remaining_points,
                s.completed_points,
                float(s.ideal_points),
                s.completed_task_count,
                s.remaining_task_count,
            )
            for s in session.execute(select(SprintBurndownSnapshot)).scalars()
        }

    svc.reconcile(sprint_id=sid)
    with sf() as session:
        v = session.execute(select(SprintVelocity)).scalar_one()
        after_rollup = (
            v.committed_points,
            v.completed_points,
            v.carryover_points,
            v.added_points,
            v.removed_points,
            v.completed_task_count,
            v.carryover_task_count,
        )
        after_snaps = {
            s.snapshot_date: (
                s.scope_points,
                s.remaining_points,
                s.completed_points,
                float(s.ideal_points),
                s.completed_task_count,
                s.remaining_task_count,
            )
            for s in session.execute(select(SprintBurndownSnapshot)).scalars()
        }
    assert before_rollup == after_rollup
    # Every originally-captured day reconciles byte-identically.
    for day, vals in before_snaps.items():
        assert after_snaps[day] == vals


class _RecordingDispatcher:
    """Test double for :class:`forge_contracts.automation.AutomationDispatcher`."""

    def __init__(self) -> None:
        self.dispatched: list[AutomationTriggerEnvelope] = []

    def dispatch(self, envelope: AutomationTriggerEnvelope) -> None:
        self.dispatched.append(envelope)


def test_start_dispatches_a_sprint_started_automation_trigger(sf: sessionmaker[Session]) -> None:
    """F40: ``start`` fires the sprint-lifecycle producer the automation engine
    needs to ever run a ``sprint_started``-triggered rule in production."""
    with sf() as session:
        sprint = _make_sprint(session)
        sid, pid = sprint.id, sprint.project_id
        session.commit()

    dispatcher = _RecordingDispatcher()
    svc = SprintService(sf, dispatcher=dispatcher)
    svc.start(workspace_id=WS, sprint_id=sid)

    assert len(dispatcher.dispatched) == 1
    envelope = dispatcher.dispatched[0]
    assert envelope.trigger_type == AutomationTriggerType.SPRINT_STARTED
    assert envelope.entity_type == AutomationEntityType.SPRINT
    assert envelope.entity_id == sid
    assert envelope.workspace_id == WS
    assert envelope.project_id == pid


def test_complete_dispatches_a_sprint_completed_automation_trigger(
    sf: sessionmaker[Session],
) -> None:
    with sf() as session:
        sprint = _make_sprint(session, status=SprintState.ACTIVE)
        sid = sprint.id
        session.commit()

    dispatcher = _RecordingDispatcher()
    svc = SprintService(sf, dispatcher=dispatcher)
    svc.complete(workspace_id=WS, sprint_id=sid)

    assert len(dispatcher.dispatched) == 1
    assert dispatcher.dispatched[0].trigger_type == AutomationTriggerType.SPRINT_COMPLETED
    assert dispatcher.dispatched[0].entity_id == sid


def test_default_dispatcher_is_a_no_op(sf: sessionmaker[Session]) -> None:
    """No ``dispatcher=`` kwarg (the CLI, most tests) never raises or blocks."""
    with sf() as session:
        sprint = _make_sprint(session)
        sid = sprint.id
        session.commit()

    svc = SprintService(sf)
    view = svc.start(workspace_id=WS, sprint_id=sid)
    assert view.state == SprintState.ACTIVE
