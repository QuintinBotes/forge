"""DB-backed sprint lifecycle + velocity service (F26).

This is the persistence/orchestration layer shared by the API router
(``apps/api``) and the Celery worker (``apps/worker``) — both depend on
``forge_board`` and ``forge_db`` but the worker cannot import ``forge_api``, so
the shared logic lives here (the slice's ``board_core/services/{sprints,velocity}``).

Design / foundation deviations (see slice notes):

* The real foundation board service is in-memory and there is no DB-backed F01
  task-write service to hook into, and ``Task.status`` is the :class:`TaskStatus`
  enum (no ``status_id`` / status-category table). So F26 owns the DB-backed
  *scope-affecting* task mutations (add/remove/complete/reopen/estimate) and
  derives "done" from ``TaskStatus.DONE`` / cancelled from ``TaskStatus.CANCELLED``.
* The persisted ``sprint_velocity`` rollup and ``sprint_burndown_snapshot`` series
  are **derived purely from the append-only ``sprint_scope_event`` log** (+ the
  ``committed_*`` snapshot frozen at start), never from current task membership.
  This makes them robust to retroactive estimate edits and to completion-time
  carryover moves, and makes ``reconcile`` byte-identical to the live path.

Sessions are synchronous (``sqlalchemy.orm.Session``), matching the foundation.
All reads/writes are workspace-scoped (a foreign id is 404, no existence leak).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from forge_board.capacity import (
    MemberAllocation,
    MemberAssignment,
    MemberCapacityInput,
    compute_capacity_report,
)
from forge_board.estimation import (
    EstimateChange,
    is_valid_estimate,
    nearest_scale_value,
)
from forge_board.estimation import EstimationScale as EstimationScaleRule
from forge_board.exceptions import ActiveSprintExistsError, SprintStateError
from forge_board.goal_alignment import (
    GoalAlignmentResult,
    TaskAlignmentInput,
    compute_goal_alignment,
)
from forge_board.portfolio import (
    CFDPoint,
    CycleLeadTime,
    PortfolioVelocitySummary,
    TaskStatusEventInput,
    average_cycle_lead_time,
    compute_cfd,
    compute_cycle_lead_time,
    compute_portfolio_velocity,
)
from forge_board.sprint_state import SprintStateMachine
from forge_board.velocity import (
    BurndownPoint,
    VelocityResult,
    VelocitySummary,
    WorkCalendar,
    compute_velocity_summary,
    ideal_line,
)
from forge_contracts.automation import (
    AutomationDispatcher,
    AutomationEntityType,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
    NullAutomationDispatcher,
)

# Import the enums from forge_db.models.enums so they are the *same* classes the
# ORM columns use (forge_db re-exports the F26 sprint enums from the frozen
# contracts; ``TaskStatus`` is forge_db's own — matching ``Task.status``).
from forge_db.models import (
    EstimationScale as EstimationScaleRow,
)
from forge_db.models import (
    Sprint,
    SprintBurndownSnapshot,
    SprintMemberCapacity,
    SprintScopeEvent,
    SprintVelocity,
    Task,
    TaskEstimateEvent,
    TaskStatusEvent,
)
from forge_db.models.enums import (
    CarryoverTarget,
    ScopeActorKind,
    SprintScopeEventType,
    SprintState,
    TaskStatus,
)

# "Done" / "cancelled" signal in this foundation (no status-category table).
COMPLETED_STATUSES = frozenset({TaskStatus.DONE})
CANCELLED_STATUSES = frozenset({TaskStatus.CANCELLED})


def _now() -> datetime:
    return datetime.now(UTC)


def _as_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _as_dt(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Errors (mapped to HTTP by the router)                                        #
# --------------------------------------------------------------------------- #


class SprintNotFound(LookupError):
    """A sprint/project/task id is absent in the caller's workspace (404)."""


class InvalidSprintRequest(ValueError):
    """A request body is structurally invalid (422)."""


# --------------------------------------------------------------------------- #
# View models (returned to the router; the API schemas wrap/re-export these)   #
# --------------------------------------------------------------------------- #


class SprintView(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    goal: str | None = None
    state: SprintState
    start_date: date | None = None
    end_date: date | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    capacity_points: int | None = None
    committed_points: int = 0
    committed_task_count: int = 0
    completed_points: int = 0
    added_points: int = 0
    removed_points: int = 0
    carryover_points: int = 0
    remaining_points: int = 0
    predictability: float = 0.0
    scope_change_ratio: float = 0.0
    velocity_version: int = 0
    # F40 PM depth: the working-day/holiday calendar (see ``WorkCalendar``).
    calendar_weekend_days: list[int] = []
    calendar_holidays: list[date] = []


class SprintReportTaskView(BaseModel):
    task_id: uuid.UUID
    key: str
    title: str
    points: int
    bucket: str


class SprintReportView(BaseModel):
    sprint: SprintView
    velocity: VelocityResult
    completed: list[SprintReportTaskView] = []
    carryover: list[SprintReportTaskView] = []
    added: list[SprintReportTaskView] = []
    removed: list[SprintReportTaskView] = []


class BurndownSeriesView(BaseModel):
    sprint_id: uuid.UUID
    start_date: date | None
    end_date: date | None
    committed_points: int
    points: list[BurndownPoint] = []


class VelocitySprintBarView(BaseModel):
    sprint_id: uuid.UUID
    name: str
    end_date: date | None
    committed_points: int
    completed_points: int
    predictability: float


class VelocityDashboardView(BaseModel):
    project_id: uuid.UUID
    sprints: list[VelocitySprintBarView] = []
    summary: VelocitySummary = VelocitySummary()


class EstimationScaleView(BaseModel):
    """A configurable estimation scale row (F40 PM depth).

    ``project_id is None`` is the workspace-wide default scale that
    :meth:`SprintService.set_task_estimate` falls back to when a task's
    project has none of its own.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str
    unit: str = "points"
    values: list[float] = []
    is_default: bool = False


class SprintService:
    """Sprint lifecycle, scope capture, velocity/burndown — over a sync session."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        dispatcher: AutomationDispatcher | None = None,
    ) -> None:
        self._sf = session_factory
        self._sm = SprintStateMachine()
        # F40: fires SPRINT_STARTED / SPRINT_COMPLETED post-commit — the
        # automation engine's sprint-lifecycle producer (see ``start``/
        # ``complete``). Callers that don't care about automations (tests, the
        # CLI) get the no-op default; the API router wires the real dispatcher.
        self._dispatcher = dispatcher or NullAutomationDispatcher()

    def _dispatch_sprint_event(self, sprint: Sprint, trigger_type: AutomationTriggerType) -> None:
        envelope = AutomationTriggerEnvelope(
            trigger_type=trigger_type,
            trigger_source=AutomationTriggerSource.BOARD_ACTIVITY,
            trigger_event_id=uuid.uuid4(),
            workspace_id=sprint.workspace_id,
            project_id=sprint.project_id,
            entity_type=AutomationEntityType.SPRINT,
            entity_id=sprint.id,
            change={"state": sprint.status},
        )
        self._dispatcher.dispatch(envelope)

    # ------------------------------------------------------------------ #
    # internal loaders                                                    #
    # ------------------------------------------------------------------ #

    def _sprint(self, session: Session, workspace_id: uuid.UUID, sprint_id: uuid.UUID) -> Sprint:
        row = session.get(Sprint, sprint_id)
        if row is None or row.workspace_id != workspace_id:
            raise SprintNotFound(str(sprint_id))
        return row

    def _task(self, session: Session, workspace_id: uuid.UUID, task_id: uuid.UUID) -> Task:
        row = session.get(Task, task_id)
        if row is None or row.workspace_id != workspace_id:
            raise SprintNotFound(str(task_id))
        return row

    @staticmethod
    def _calendar_for(sprint: Sprint) -> WorkCalendar:
        """Build the sprint's :class:`WorkCalendar` from its stored columns.

        Malformed holiday entries are skipped rather than raising — the
        calendar is a planning aid, not a source of truth the burndown must
        fail closed on.
        """
        holidays: set[date] = set()
        for entry in sprint.calendar_holidays or []:
            if isinstance(entry, date):
                holidays.add(entry)
                continue
            try:
                holidays.add(date.fromisoformat(str(entry)))
            except ValueError:
                continue
        weekend_days = {
            int(d) for d in (sprint.calendar_weekend_days or []) if str(d).lstrip("-").isdigit()
        }
        return WorkCalendar(weekend_days=frozenset(weekend_days), holidays=frozenset(holidays))

    def _events(self, session: Session, sprint_id: uuid.UUID) -> list[SprintScopeEvent]:
        rows = list(
            session.execute(
                select(SprintScopeEvent).where(SprintScopeEvent.sprint_id == sprint_id)
            ).scalars()
        )
        rows.sort(key=lambda e: (e.occurred_at, e.created_at))
        return rows

    # ------------------------------------------------------------------ #
    # scope-event recording (append-only)                                 #
    # ------------------------------------------------------------------ #

    def _record_event(
        self,
        session: Session,
        sprint: Sprint,
        event_type: SprintScopeEventType,
        *,
        task_id: uuid.UUID | None = None,
        points_before: int | None = None,
        points_after: int | None = None,
        actor_kind: ScopeActorKind = ScopeActorKind.SYSTEM,
        actor_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> SprintScopeEvent:
        events = self._events(session, sprint.id)
        if events:
            scope = events[-1].scope_points_after
            remaining = events[-1].remaining_points_after
        else:
            scope = remaining = sprint.committed_points

        before = points_before or 0
        after = points_after or 0
        delta = 0
        if event_type == SprintScopeEventType.SPRINT_STARTED:
            scope = remaining = sprint.committed_points
            delta = sprint.committed_points
        elif event_type == SprintScopeEventType.TASK_ADDED:
            delta = after
            scope += after
            remaining += after
        elif event_type == SprintScopeEventType.TASK_REMOVED:
            delta = -before
            scope -= before
            remaining -= before
        elif event_type == SprintScopeEventType.TASK_COMPLETED:
            delta = -before
            remaining -= before
        elif event_type == SprintScopeEventType.TASK_REOPENED:
            delta = after
            remaining += after
        elif event_type == SprintScopeEventType.ESTIMATE_CHANGED:
            delta = after - before
            scope += delta
            remaining += delta
        # SPRINT_COMPLETED / SPRINT_CANCELLED are markers (no delta).

        row = SprintScopeEvent(
            workspace_id=sprint.workspace_id,
            project_id=sprint.project_id,
            sprint_id=sprint.id,
            task_id=task_id,
            event_type=event_type,
            points_delta=delta,
            points_before=points_before,
            points_after=points_after,
            scope_points_after=scope,
            remaining_points_after=remaining,
            actor_kind=actor_kind,
            actor_id=actor_id,
            occurred_at=occurred_at or _now(),
        )
        session.add(row)
        session.flush()
        return row

    # ------------------------------------------------------------------ #
    # rollup (derived purely from the event log + committed snapshot)     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _reconstruct_counts(
        rows: list[SprintScopeEvent], committed_task_count: int
    ) -> tuple[int, int, int]:
        scope = remaining = completed = 0
        for ev in rows:
            t = ev.event_type
            if t == SprintScopeEventType.SPRINT_STARTED:
                scope = remaining = committed_task_count
                completed = 0
            elif t == SprintScopeEventType.TASK_ADDED:
                scope += 1
                remaining += 1
            elif t == SprintScopeEventType.TASK_REMOVED:
                scope -= 1
                remaining -= 1
            elif t == SprintScopeEventType.TASK_COMPLETED:
                completed += 1
                remaining -= 1
            elif t == SprintScopeEventType.TASK_REOPENED:
                completed -= 1
                remaining += 1
        return scope, remaining, completed

    def _compute_result(self, sprint: Sprint, rows: list[SprintScopeEvent]) -> VelocityResult:
        committed = sprint.committed_points
        committed_tc = sprint.committed_task_count
        if rows:
            scope_final = rows[-1].scope_points_after
            remaining_final = rows[-1].remaining_points_after
        else:
            scope_final = remaining_final = committed
        completed_points = scope_final - remaining_final
        carryover_points = remaining_final
        added_points = sum(
            e.points_delta for e in rows if e.event_type == SprintScopeEventType.TASK_ADDED
        )
        removed_points = sum(
            -e.points_delta for e in rows if e.event_type == SprintScopeEventType.TASK_REMOVED
        )
        _scope_tc, remaining_tc, completed_tc = self._reconstruct_counts(rows, committed_tc)
        predictability = round(completed_points / committed, 4) if committed else 0.0
        scope_change_ratio = (
            round((added_points + removed_points) / committed, 4) if committed else 0.0
        )
        return VelocityResult(
            committed_points=committed,
            completed_points=completed_points,
            added_points=added_points,
            removed_points=removed_points,
            carryover_points=carryover_points,
            committed_task_count=committed_tc,
            completed_task_count=completed_tc,
            carryover_task_count=remaining_tc,
            predictability=predictability,
            scope_change_ratio=scope_change_ratio,
        )

    def _recompute(self, session: Session, sprint: Sprint) -> SprintVelocity:
        rows = self._events(session, sprint.id)
        result = self._compute_result(sprint, rows)
        row = session.execute(
            select(SprintVelocity).where(SprintVelocity.sprint_id == sprint.id)
        ).scalar_one_or_none()
        if row is None:
            row = SprintVelocity(
                workspace_id=sprint.workspace_id,
                project_id=sprint.project_id,
                sprint_id=sprint.id,
            )
            session.add(row)
        row.committed_points = result.committed_points
        row.completed_points = result.completed_points
        row.added_points = result.added_points
        row.removed_points = result.removed_points
        row.carryover_points = result.carryover_points
        row.committed_task_count = result.committed_task_count
        row.completed_task_count = result.completed_task_count
        row.carryover_task_count = result.carryover_task_count
        row.predictability = result.predictability
        row.scope_change_ratio = result.scope_change_ratio
        row.state = sprint.status
        row.computed_at = _now()
        sprint.velocity_version = (sprint.velocity_version or 0) + 1
        session.flush()
        return row

    # ------------------------------------------------------------------ #
    # burndown snapshot (idempotent per (sprint, day))                    #
    # ------------------------------------------------------------------ #

    def _snapshot_for_day(
        self, session: Session, sprint: Sprint, rows: list[SprintScopeEvent], day: date
    ) -> SprintBurndownSnapshot:
        committed = sprint.committed_points
        committed_tc = sprint.committed_task_count
        start = _as_date(sprint.start_date) or day
        end = _as_date(sprint.end_date) or start
        applied = [e for e in rows if e.occurred_at.date() <= day]
        if applied:
            scope = applied[-1].scope_points_after
            remaining = applied[-1].remaining_points_after
            _s, remaining_tc, completed_tc = self._reconstruct_counts(applied, committed_tc)
        else:
            scope = remaining = committed
            remaining_tc = committed_tc
            completed_tc = 0
        ideal = ideal_line(committed, start, end, self._calendar_for(sprint)).get(day, 0.0)

        existing = session.execute(
            select(SprintBurndownSnapshot).where(
                SprintBurndownSnapshot.sprint_id == sprint.id,
                SprintBurndownSnapshot.snapshot_date == day,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = SprintBurndownSnapshot(
                workspace_id=sprint.workspace_id,
                project_id=sprint.project_id,
                sprint_id=sprint.id,
                snapshot_date=day,
            )
            session.add(existing)
        existing.scope_points = scope
        existing.remaining_points = remaining
        existing.completed_points = scope - remaining
        existing.ideal_points = ideal
        existing.completed_task_count = completed_tc
        existing.remaining_task_count = remaining_tc
        session.flush()
        return existing

    # ------------------------------------------------------------------ #
    # view assembly                                                       #
    # ------------------------------------------------------------------ #

    def _view(self, session: Session, sprint: Sprint) -> SprintView:
        rollup = session.execute(
            select(SprintVelocity).where(SprintVelocity.sprint_id == sprint.id)
        ).scalar_one_or_none()
        v = rollup
        return SprintView(
            id=sprint.id,
            project_id=sprint.project_id,
            workspace_id=sprint.workspace_id,
            name=sprint.name,
            goal=sprint.goal,
            state=SprintState(sprint.status),
            start_date=_as_date(sprint.start_date),
            end_date=_as_date(sprint.end_date),
            started_at=sprint.started_at,
            completed_at=sprint.completed_at,
            capacity_points=sprint.capacity_points,
            committed_points=sprint.committed_points,
            committed_task_count=sprint.committed_task_count,
            completed_points=int(v.completed_points) if v else 0,
            added_points=int(v.added_points) if v else 0,
            removed_points=int(v.removed_points) if v else 0,
            carryover_points=int(v.carryover_points) if v else 0,
            remaining_points=int(v.carryover_points) if v else sprint.committed_points,
            predictability=float(v.predictability) if v else 0.0,
            scope_change_ratio=float(v.scope_change_ratio) if v else 0.0,
            velocity_version=sprint.velocity_version,
            calendar_weekend_days=list(sprint.calendar_weekend_days or []),
            calendar_holidays=[
                d if isinstance(d, date) else date.fromisoformat(str(d))
                for d in (sprint.calendar_holidays or [])
            ],
        )

    # ------------------------------------------------------------------ #
    # CRUD + lifecycle                                                    #
    # ------------------------------------------------------------------ #

    def create(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        name: str,
        goal: str | None = None,
        start_date: date,
        end_date: date,
        capacity_points: int | None = None,
        calendar_weekend_days: list[int] | None = None,
        calendar_holidays: list[date] | None = None,
    ) -> SprintView:
        if end_date < start_date:
            raise InvalidSprintRequest("end_date must be >= start_date")
        with self._sf() as session:
            sprint = Sprint(
                workspace_id=workspace_id,
                project_id=project_id,
                name=name,
                goal=goal,
                start_date=_as_dt(start_date),
                end_date=_as_dt(end_date),
                status=SprintState.PLANNED.value,
                capacity_points=capacity_points,
                calendar_weekend_days=list(calendar_weekend_days or []),
                calendar_holidays=[d.isoformat() for d in (calendar_holidays or [])],
            )
            session.add(sprint)
            session.commit()
            session.refresh(sprint)
            return self._view(session, sprint)

    def update(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
        name: str | None = None,
        goal: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        capacity_points: int | None = None,
        calendar_weekend_days: list[int] | None = None,
        calendar_holidays: list[date] | None = None,
    ) -> SprintView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            if SprintState(sprint.status) in (SprintState.COMPLETED, SprintState.CANCELLED):
                raise SprintStateError(sprint.status, "edit")
            if name is not None:
                sprint.name = name
            if goal is not None:
                sprint.goal = goal
            if start_date is not None:
                sprint.start_date = _as_dt(start_date)
            if end_date is not None:
                sprint.end_date = _as_dt(end_date)
            if capacity_points is not None:
                sprint.capacity_points = capacity_points
            if calendar_weekend_days is not None:
                sprint.calendar_weekend_days = list(calendar_weekend_days)
            if calendar_holidays is not None:
                sprint.calendar_holidays = [d.isoformat() for d in calendar_holidays]
            new_start = _as_date(sprint.start_date)
            new_end = _as_date(sprint.end_date)
            if new_start and new_end and new_end < new_start:
                raise InvalidSprintRequest("end_date must be >= start_date")
            session.commit()
            session.refresh(sprint)
            return self._view(session, sprint)

    def start(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> SprintView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            self._sm.assert_transition(SprintState(sprint.status), SprintState.ACTIVE)
            existing = session.execute(
                select(Sprint.id).where(
                    Sprint.project_id == sprint.project_id,
                    Sprint.status == SprintState.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ActiveSprintExistsError(existing)

            tasks = list(session.execute(select(Task).where(Task.sprint_id == sprint.id)).scalars())
            committed = [
                t
                for t in tasks
                if t.status not in COMPLETED_STATUSES and t.status not in CANCELLED_STATUSES
            ]
            sprint.committed_points = sum(t.estimate or 0 for t in committed)
            sprint.committed_task_count = len(committed)
            sprint.committed_task_ids = [
                {"id": str(t.id), "points": t.estimate or 0} for t in committed
            ]
            sprint.status = SprintState.ACTIVE.value
            sprint.started_at = _now()
            session.flush()

            self._record_event(
                session,
                sprint,
                SprintScopeEventType.SPRINT_STARTED,
                actor_id=actor_id,
                actor_kind=ScopeActorKind.USER if actor_id else ScopeActorKind.SYSTEM,
                occurred_at=sprint.started_at,
            )
            self._recompute(session, sprint)
            rows = self._events(session, sprint.id)
            # Day-0 burndown point is keyed on the sprint's logical start_date (so
            # it sits inside the window and the reconcile replay reproduces it).
            day0 = _as_date(sprint.start_date) or _as_date(sprint.started_at) or date.today()
            self._snapshot_for_day(session, sprint, rows, day0)
            session.commit()
            session.refresh(sprint)
            self._dispatch_sprint_event(sprint, AutomationTriggerType.SPRINT_STARTED)
            return self._view(session, sprint)

    def cancel(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> SprintView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            self._sm.assert_transition(SprintState(sprint.status), SprintState.CANCELLED)
            # Return tasks to the backlog.
            for task in session.execute(select(Task).where(Task.sprint_id == sprint.id)).scalars():
                task.sprint_id = None
            sprint.status = SprintState.CANCELLED.value
            sprint.completed_at = _now()
            session.flush()
            self._record_event(
                session,
                sprint,
                SprintScopeEventType.SPRINT_CANCELLED,
                actor_id=actor_id,
                actor_kind=ScopeActorKind.USER if actor_id else ScopeActorKind.SYSTEM,
            )
            self._recompute(session, sprint)
            session.commit()
            session.refresh(sprint)
            return self._view(session, sprint)

    def complete(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
        carryover: CarryoverTarget = CarryoverTarget.BACKLOG,
        next_sprint_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> SprintReportView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            self._sm.assert_transition(SprintState(sprint.status), SprintState.COMPLETED)
            if carryover == CarryoverTarget.NEXT_SPRINT:
                if next_sprint_id is None:
                    raise InvalidSprintRequest("next_sprint_id required for next_sprint carryover")
                target = self._sprint(session, workspace_id, next_sprint_id)
                if target.project_id != sprint.project_id:
                    raise SprintNotFound(str(next_sprint_id))

            # Finalize the rollup from the event log *before* moving carryover.
            sprint.status = SprintState.COMPLETED.value
            sprint.completed_at = _now()
            session.flush()
            self._record_event(
                session,
                sprint,
                SprintScopeEventType.SPRINT_COMPLETED,
                actor_id=actor_id,
                actor_kind=ScopeActorKind.USER if actor_id else ScopeActorKind.SYSTEM,
            )
            self._recompute(session, sprint)

            # Move incomplete (non-done, non-cancelled) tasks per the chosen target.
            incomplete = [
                t
                for t in session.execute(select(Task).where(Task.sprint_id == sprint.id)).scalars()
                if t.status not in COMPLETED_STATUSES and t.status not in CANCELLED_STATUSES
            ]
            if carryover == CarryoverTarget.BACKLOG:
                for t in incomplete:
                    t.sprint_id = None
            elif carryover == CarryoverTarget.NEXT_SPRINT:
                for t in incomplete:
                    t.sprint_id = next_sprint_id
            # LEAVE: keep as-is.
            session.commit()
            session.refresh(sprint)
            self._dispatch_sprint_event(sprint, AutomationTriggerType.SPRINT_COMPLETED)
            return self._report(session, sprint)

    def recompute(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
    ) -> SprintView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            self._recompute(session, sprint)
            session.commit()
            session.refresh(sprint)
            return self._view(session, sprint)

    # ------------------------------------------------------------------ #
    # scope capture (mutate task + record; only when sprint is active)    #
    # ------------------------------------------------------------------ #

    def add_task(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
        task_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
        actor_kind: ScopeActorKind = ScopeActorKind.USER,
    ) -> SprintView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            task = self._task(session, workspace_id, task_id)
            task.sprint_id = sprint.id
            session.flush()
            if SprintState(sprint.status) == SprintState.ACTIVE:
                self._record_event(
                    session,
                    sprint,
                    SprintScopeEventType.TASK_ADDED,
                    task_id=task.id,
                    points_after=task.estimate or 0,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                self._recompute(session, sprint)
            session.commit()
            session.refresh(sprint)
            return self._view(session, sprint)

    def remove_task(
        self,
        *,
        workspace_id: uuid.UUID,
        task_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
        actor_kind: ScopeActorKind = ScopeActorKind.USER,
    ) -> None:
        with self._sf() as session:
            task = self._task(session, workspace_id, task_id)
            sprint = session.get(Sprint, task.sprint_id) if task.sprint_id else None
            task.sprint_id = None
            session.flush()
            if sprint is not None and SprintState(sprint.status) == SprintState.ACTIVE:
                self._record_event(
                    session,
                    sprint,
                    SprintScopeEventType.TASK_REMOVED,
                    task_id=task.id,
                    points_before=task.estimate or 0,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                self._recompute(session, sprint)
            session.commit()

    def set_task_status(
        self,
        *,
        workspace_id: uuid.UUID,
        task_id: uuid.UUID,
        status: TaskStatus,
        actor_id: uuid.UUID | None = None,
        actor_kind: ScopeActorKind = ScopeActorKind.USER,
    ) -> None:
        with self._sf() as session:
            task = self._task(session, workspace_id, task_id)
            old = task.status
            sprint = session.get(Sprint, task.sprint_id) if task.sprint_id else None
            task.status = status
            session.flush()
            # F40 PM depth: append-only status-transition log, recorded on *every*
            # move regardless of sprint membership/state — the source the
            # portfolio CFD and cycle/lead-time rollups read from (distinct from
            # the F26 scope event above, which only fires while the sprint is
            # active and only around the DONE boundary).
            if old != status:
                session.add(
                    TaskStatusEvent(
                        workspace_id=workspace_id,
                        task_id=task.id,
                        project_id=task.project_id,
                        sprint_id=task.sprint_id,
                        from_status=str(old),
                        to_status=str(status),
                        actor_id=actor_id,
                        changed_at=_now(),
                    )
                )
            if sprint is not None and SprintState(sprint.status) == SprintState.ACTIVE:
                crossed_in = old not in COMPLETED_STATUSES and status in COMPLETED_STATUSES
                crossed_out = old in COMPLETED_STATUSES and status not in COMPLETED_STATUSES
                if crossed_in:
                    self._record_event(
                        session,
                        sprint,
                        SprintScopeEventType.TASK_COMPLETED,
                        task_id=task.id,
                        points_before=task.estimate or 0,
                        actor_id=actor_id,
                        actor_kind=actor_kind,
                    )
                    self._recompute(session, sprint)
                elif crossed_out:
                    self._record_event(
                        session,
                        sprint,
                        SprintScopeEventType.TASK_REOPENED,
                        task_id=task.id,
                        points_after=task.estimate or 0,
                        actor_id=actor_id,
                        actor_kind=actor_kind,
                    )
                    self._recompute(session, sprint)
            session.commit()

    def _applicable_estimation_scale(
        self, session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> EstimationScaleRow | None:
        """The scale governing ``project_id``: its own default, else the
        workspace-wide default, else ``None`` (unrestricted)."""
        row = session.execute(
            select(EstimationScaleRow).where(
                EstimationScaleRow.workspace_id == workspace_id,
                EstimationScaleRow.project_id == project_id,
                EstimationScaleRow.is_default.is_(True),
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        return session.execute(
            select(EstimationScaleRow).where(
                EstimationScaleRow.workspace_id == workspace_id,
                EstimationScaleRow.project_id.is_(None),
                EstimationScaleRow.is_default.is_(True),
            )
        ).scalar_one_or_none()

    def _snap_estimate_to_scale(
        self,
        session: Session,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        estimate: int | None,
    ) -> int | None:
        """Snap ``estimate`` to the project's configured scale, if any.

        No default scale configured (for the project or the workspace), or a
        scale with no declared ``values`` -> unrestricted, ``estimate`` passes
        through untouched (this is the case every pre-F40 caller hits).
        """
        if estimate is None:
            return None
        row = self._applicable_estimation_scale(session, workspace_id, project_id)
        if row is None or not row.values:
            return estimate
        rule = EstimationScaleRule(
            name=row.name,
            unit=row.unit,
            values=[float(v) for v in row.values],
            is_default=row.is_default,
        )
        if is_valid_estimate(rule, float(estimate)):
            return estimate
        return round(nearest_scale_value(rule, float(estimate)))

    def set_task_estimate(
        self,
        *,
        workspace_id: uuid.UUID,
        task_id: uuid.UUID,
        estimate: int | None,
        actor_id: uuid.UUID | None = None,
        actor_kind: ScopeActorKind = ScopeActorKind.USER,
    ) -> None:
        with self._sf() as session:
            task = self._task(session, workspace_id, task_id)
            before = task.estimate or 0
            sprint = session.get(Sprint, task.sprint_id) if task.sprint_id else None
            estimate = self._snap_estimate_to_scale(
                session, workspace_id, task.project_id, estimate
            )
            task.estimate = estimate
            after = estimate or 0
            session.flush()
            if before != after:
                # F40 PM depth: durable, always-on estimate-change history —
                # recorded regardless of sprint state (unlike the F26
                # ESTIMATE_CHANGED scope event below, which only fires while the
                # task's sprint is active).
                session.add(
                    TaskEstimateEvent(
                        workspace_id=workspace_id,
                        task_id=task.id,
                        sprint_id=task.sprint_id,
                        points_before=before,
                        points_after=after,
                        actor_id=actor_id,
                        changed_at=_now(),
                    )
                )
            if (
                sprint is not None
                and SprintState(sprint.status) == SprintState.ACTIVE
                and before != after
            ):
                self._record_event(
                    session,
                    sprint,
                    SprintScopeEventType.ESTIMATE_CHANGED,
                    task_id=task.id,
                    points_before=before,
                    points_after=after,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                self._recompute(session, sprint)
            session.commit()

    def estimate_history(
        self, *, workspace_id: uuid.UUID, task_id: uuid.UUID
    ) -> list[EstimateChange]:
        """A task's full estimate-change history, oldest first."""
        with self._sf() as session:
            self._task(session, workspace_id, task_id)  # 404 if foreign/missing
            rows = session.execute(
                select(TaskEstimateEvent)
                .where(TaskEstimateEvent.task_id == task_id)
                .order_by(TaskEstimateEvent.changed_at)
            ).scalars()
            return [
                EstimateChange(
                    task_id=str(task_id),
                    points_before=r.points_before,
                    points_after=r.points_after,
                    changed_at=r.changed_at,
                    actor_id=str(r.actor_id) if r.actor_id else None,
                )
                for r in rows
            ]

    # ------------------------------------------------------------------ #
    # configurable estimation scales (F40 PM depth)                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _estimation_scale_view(row: EstimationScaleRow) -> EstimationScaleView:
        return EstimationScaleView(
            id=row.id,
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            name=row.name,
            unit=row.unit,
            values=[float(v) for v in (row.values or [])],
            is_default=row.is_default,
        )

    def create_estimation_scale(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID | None = None,
        name: str,
        unit: str = "points",
        values: list[float] | None = None,
        is_default: bool = False,
    ) -> EstimationScaleView:
        with self._sf() as session:
            row = EstimationScaleRow(
                workspace_id=workspace_id,
                project_id=project_id,
                name=name,
                unit=unit,
                values=list(values or []),
                is_default=is_default,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._estimation_scale_view(row)

    def list_estimation_scales(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID | None = None
    ) -> list[EstimationScaleView]:
        """Scales visible to ``project_id``: its own plus the workspace-wide
        ones. ``project_id=None`` lists only the workspace-wide scales."""
        with self._sf() as session:
            q = select(EstimationScaleRow).where(EstimationScaleRow.workspace_id == workspace_id)
            if project_id is None:
                q = q.where(EstimationScaleRow.project_id.is_(None))
            else:
                q = q.where(
                    or_(
                        EstimationScaleRow.project_id == project_id,
                        EstimationScaleRow.project_id.is_(None),
                    )
                )
            q = q.order_by(EstimationScaleRow.created_at)
            return [self._estimation_scale_view(r) for r in session.execute(q).scalars()]

    def update_estimation_scale(
        self,
        *,
        workspace_id: uuid.UUID,
        scale_id: uuid.UUID,
        name: str | None = None,
        unit: str | None = None,
        values: list[float] | None = None,
        is_default: bool | None = None,
    ) -> EstimationScaleView:
        with self._sf() as session:
            row = session.get(EstimationScaleRow, scale_id)
            if row is None or row.workspace_id != workspace_id:
                raise SprintNotFound(str(scale_id))
            if name is not None:
                row.name = name
            if unit is not None:
                row.unit = unit
            if values is not None:
                row.values = list(values)
            if is_default is not None:
                row.is_default = is_default
            session.commit()
            session.refresh(row)
            return self._estimation_scale_view(row)

    # ------------------------------------------------------------------ #
    # per-member capacity (F40 PM depth)                                   #
    # ------------------------------------------------------------------ #

    def set_member_capacity(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
        member_id: uuid.UUID,
        capacity_points: float,
    ) -> None:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            row = session.execute(
                select(SprintMemberCapacity).where(
                    SprintMemberCapacity.sprint_id == sprint.id,
                    SprintMemberCapacity.member_id == member_id,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SprintMemberCapacity(
                    workspace_id=workspace_id, sprint_id=sprint.id, member_id=member_id
                )
                session.add(row)
            row.capacity_points = capacity_points
            session.commit()

    def capacity_report(
        self, *, workspace_id: uuid.UUID, sprint_id: uuid.UUID
    ) -> list[MemberAllocation]:
        """Each member's declared capacity vs. their assigned committed-task points."""
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            capacities = [
                MemberCapacityInput(
                    member_id=str(r.member_id), capacity_points=float(r.capacity_points)
                )
                for r in session.execute(
                    select(SprintMemberCapacity).where(SprintMemberCapacity.sprint_id == sprint.id)
                ).scalars()
            ]
            assignments = [
                MemberAssignment(member_id=str(t.assignee_id), points=t.estimate or 0)
                for t in session.execute(
                    select(Task).where(Task.sprint_id == sprint.id, Task.assignee_id.is_not(None))
                ).scalars()
            ]
            return compute_capacity_report(capacities, assignments)

    # ------------------------------------------------------------------ #
    # portfolio: CFD, cycle/lead time, cross-project velocity (F40)        #
    # ------------------------------------------------------------------ #

    def cfd(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        start: date,
        end: date,
    ) -> list[CFDPoint]:
        """Cumulative Flow Diagram over ``[start, end]`` for a project."""
        with self._sf() as session:
            rows = session.execute(
                select(TaskStatusEvent)
                .where(
                    TaskStatusEvent.workspace_id == workspace_id,
                    TaskStatusEvent.project_id == project_id,
                )
                .order_by(TaskStatusEvent.changed_at)
            ).scalars()
            events = [
                TaskStatusEventInput(
                    task_id=str(r.task_id), to_status=r.to_status, changed_at=r.changed_at
                )
                for r in rows
            ]
            return compute_cfd(events, start, end)

    def cycle_lead_time(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> tuple[list[CycleLeadTime], float, float]:
        """Per-task cycle/lead time + (avg lead, avg cycle) for a project."""
        with self._sf() as session:
            rows = session.execute(
                select(TaskStatusEvent)
                .where(
                    TaskStatusEvent.workspace_id == workspace_id,
                    TaskStatusEvent.project_id == project_id,
                )
                .order_by(TaskStatusEvent.changed_at)
            ).scalars()
            events_by_task: dict[str, list[TaskStatusEventInput]] = {}
            for r in rows:
                events_by_task.setdefault(str(r.task_id), []).append(
                    TaskStatusEventInput(
                        task_id=str(r.task_id), to_status=r.to_status, changed_at=r.changed_at
                    )
                )
            created_at_by_task = {
                str(t.id): t.created_at
                for t in session.execute(
                    select(Task).where(
                        Task.workspace_id == workspace_id, Task.project_id == project_id
                    )
                ).scalars()
            }
            rows_out = compute_cycle_lead_time(events_by_task, created_at_by_task)
            avg_lead, avg_cycle = average_cycle_lead_time(rows_out)
            return rows_out, avg_lead, avg_cycle

    def portfolio_velocity(
        self, *, workspace_id: uuid.UUID, project_ids: list[uuid.UUID], last: int = 6
    ) -> PortfolioVelocitySummary:
        """Aggregate each project's independent velocity summary into one view."""
        per_project = {
            str(pid): self.velocity_dashboard(
                workspace_id=workspace_id, project_id=pid, last=last
            ).summary
            for pid in project_ids
        }
        return compute_portfolio_velocity(per_project)

    def goal_alignment(
        self, *, workspace_id: uuid.UUID, sprint_id: uuid.UUID
    ) -> GoalAlignmentResult:
        """Score the sprint goal's keyword coverage across its current tasks."""
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            tasks = session.execute(select(Task).where(Task.sprint_id == sprint.id)).scalars()
            inputs = [
                TaskAlignmentInput(
                    task_id=str(t.id),
                    title=t.title,
                    acceptance_criteria=[str(c) for c in (t.acceptance_criteria or [])],
                )
                for t in tasks
            ]
            return compute_goal_alignment(sprint.goal, inputs)

    # ------------------------------------------------------------------ #
    # worker entrypoints (snapshot / reconcile)                           #
    # ------------------------------------------------------------------ #

    def snapshot_burndown_for_active(self, snapshot_date: date | None = None) -> int:
        """Upsert one burndown row per active sprint for ``snapshot_date``."""
        day = snapshot_date or date.today()
        count = 0
        with self._sf() as session:
            for sprint in session.execute(
                select(Sprint).where(Sprint.status == SprintState.ACTIVE.value)
            ).scalars():
                rows = self._events(session, sprint.id)
                self._snapshot_for_day(session, sprint, rows, day)
                count += 1
            session.commit()
        return count

    def recompute_by_id(self, sprint_id: uuid.UUID) -> int | None:
        """Recompute one sprint's rollup (worker entrypoint; no workspace check)."""
        with self._sf() as session:
            sprint = session.get(Sprint, sprint_id)
            if sprint is None:
                return None
            self._recompute(session, sprint)
            session.commit()
            return sprint.velocity_version

    def reconcile(self, *, sprint_id: uuid.UUID, workspace_id: uuid.UUID | None = None) -> None:
        """Rebuild the rollup + replay the burndown series from the event log."""
        with self._sf() as session:
            sprint = session.get(Sprint, sprint_id)
            if sprint is None or (workspace_id is not None and sprint.workspace_id != workspace_id):
                raise SprintNotFound(str(sprint_id))
            # Drop derived state.
            for snap in session.execute(
                select(SprintBurndownSnapshot).where(SprintBurndownSnapshot.sprint_id == sprint.id)
            ).scalars():
                session.delete(snap)
            existing = session.execute(
                select(SprintVelocity).where(SprintVelocity.sprint_id == sprint.id)
            ).scalar_one_or_none()
            if existing is not None:
                session.delete(existing)
            session.flush()
            # Rebuild.
            self._recompute(session, sprint)
            rows = self._events(session, sprint.id)
            start = _as_date(sprint.start_date)
            end = _as_date(sprint.end_date)
            if start is not None and end is not None:
                last = end
                if sprint.completed_at is not None:
                    last = min(end, _as_date(sprint.completed_at) or end)
                day = start
                while day <= last:
                    self._snapshot_for_day(session, sprint, rows, day)
                    day = date.fromordinal(day.toordinal() + 1)
            session.commit()

    # ------------------------------------------------------------------ #
    # reads                                                               #
    # ------------------------------------------------------------------ #

    def get(self, *, workspace_id: uuid.UUID, sprint_id: uuid.UUID) -> SprintView:
        with self._sf() as session:
            return self._view(session, self._sprint(session, workspace_id, sprint_id))

    def list_sprints(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        state: SprintState | None = None,
        limit: int = 50,
    ) -> list[SprintView]:
        with self._sf() as session:
            q = select(Sprint).where(
                Sprint.workspace_id == workspace_id, Sprint.project_id == project_id
            )
            if state is not None:
                q = q.where(Sprint.status == state.value)
            q = q.order_by(Sprint.created_at).limit(min(limit, 250))
            return [self._view(session, s) for s in session.execute(q).scalars()]

    def burndown(
        self,
        *,
        workspace_id: uuid.UUID,
        sprint_id: uuid.UUID,
        as_of: date | None = None,
    ) -> BurndownSeriesView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            rows = self._events(session, sprint.id)
            start = _as_date(sprint.start_date)
            end = _as_date(sprint.end_date)
            points: list[BurndownPoint] = []
            if start is not None and end is not None:
                last_day = min(end, as_of) if as_of is not None else min(end, date.today())
                if SprintState(sprint.status) in (
                    SprintState.PLANNED,
                    SprintState.CANCELLED,
                ):
                    last_day = end if as_of is None else min(end, as_of)
                if last_day < start:
                    last_day = start
                day = start
                while day <= last_day:
                    points.append(self._point_view(sprint, rows, day))
                    day = date.fromordinal(day.toordinal() + 1)
            return BurndownSeriesView(
                sprint_id=sprint.id,
                start_date=start,
                end_date=end,
                committed_points=sprint.committed_points,
                points=points,
            )

    def _point_view(self, sprint: Sprint, rows: list[SprintScopeEvent], day: date) -> BurndownPoint:
        committed = sprint.committed_points
        committed_tc = sprint.committed_task_count
        start = _as_date(sprint.start_date) or day
        end = _as_date(sprint.end_date) or start
        applied = [e for e in rows if e.occurred_at.date() <= day]
        if applied:
            scope = applied[-1].scope_points_after
            remaining = applied[-1].remaining_points_after
            _s, remaining_tc, completed_tc = self._reconstruct_counts(applied, committed_tc)
        else:
            scope = remaining = committed
            remaining_tc = committed_tc
            completed_tc = 0
        return BurndownPoint(
            snapshot_date=day,
            scope_points=scope,
            remaining_points=remaining,
            completed_points=scope - remaining,
            ideal_points=ideal_line(committed, start, end, self._calendar_for(sprint)).get(
                day, 0.0
            ),
            completed_task_count=completed_tc,
            remaining_task_count=remaining_tc,
        )

    def report(self, *, workspace_id: uuid.UUID, sprint_id: uuid.UUID) -> SprintReportView:
        with self._sf() as session:
            sprint = self._sprint(session, workspace_id, sprint_id)
            return self._report(session, sprint)

    def _report(self, session: Session, sprint: Sprint) -> SprintReportView:
        rows = self._events(session, sprint.id)
        result = self._compute_result(sprint, rows)

        committed_map: dict[uuid.UUID, int] = {}
        for entry in sprint.committed_task_ids or []:
            if isinstance(entry, dict) and "id" in entry:
                committed_map[uuid.UUID(str(entry["id"]))] = int(entry.get("points", 0))

        # Per-task event aggregation.
        added_points: dict[uuid.UUID, int] = {}
        removed_points: dict[uuid.UUID, int] = {}
        net_completed: dict[uuid.UUID, int] = {}
        for ev in rows:
            if ev.task_id is None:
                continue
            tid = ev.task_id
            if ev.event_type == SprintScopeEventType.TASK_ADDED:
                added_points[tid] = added_points.get(tid, 0) + ev.points_delta
            elif ev.event_type == SprintScopeEventType.TASK_REMOVED:
                removed_points[tid] = removed_points.get(tid, 0) + (-ev.points_delta)
            elif ev.event_type == SprintScopeEventType.TASK_COMPLETED:
                net_completed[tid] = net_completed.get(tid, 0) + 1
            elif ev.event_type == SprintScopeEventType.TASK_REOPENED:
                net_completed[tid] = net_completed.get(tid, 0) - 1

        completed: list[SprintReportTaskView] = []
        carryover: list[SprintReportTaskView] = []
        added: list[SprintReportTaskView] = []
        removed: list[SprintReportTaskView] = []

        def _task_meta(tid: uuid.UUID) -> tuple[str, str]:
            t = session.get(Task, tid)
            if t is None:
                return (str(tid)[:8], "(deleted)")
            return (t.key, t.title)

        scope_ids = set(committed_map) | set(added_points)
        for tid in sorted(scope_ids, key=str):
            if tid in removed_points:
                key, title = _task_meta(tid)
                removed.append(
                    SprintReportTaskView(
                        task_id=tid,
                        key=key,
                        title=title,
                        points=removed_points[tid],
                        bucket="removed",
                    )
                )
                continue
            points = committed_map.get(tid, added_points.get(tid, 0))
            key, title = _task_meta(tid)
            if net_completed.get(tid, 0) > 0:
                completed.append(
                    SprintReportTaskView(
                        task_id=tid, key=key, title=title, points=points, bucket="completed"
                    )
                )
            else:
                carryover.append(
                    SprintReportTaskView(
                        task_id=tid, key=key, title=title, points=points, bucket="carryover"
                    )
                )
            if tid in added_points:
                added.append(
                    SprintReportTaskView(
                        task_id=tid,
                        key=key,
                        title=title,
                        points=added_points[tid],
                        bucket="added",
                    )
                )

        return SprintReportView(
            sprint=self._view(session, sprint),
            velocity=result,
            completed=completed,
            carryover=carryover,
            added=added,
            removed=removed,
        )

    def velocity_dashboard(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID, last: int = 6
    ) -> VelocityDashboardView:
        last = max(1, min(last, 26))
        with self._sf() as session:
            # Completed sprints only, oldest -> newest by completion time.
            rows = list(
                session.execute(
                    select(Sprint, SprintVelocity)
                    .join(SprintVelocity, SprintVelocity.sprint_id == Sprint.id)
                    .where(
                        Sprint.workspace_id == workspace_id,
                        Sprint.project_id == project_id,
                        Sprint.status == SprintState.COMPLETED.value,
                    )
                ).all()
            )
            rows.sort(key=lambda r: r[0].completed_at or r[0].created_at)
            rows = rows[-last:]
            bars = [
                VelocitySprintBarView(
                    sprint_id=s.id,
                    name=s.name,
                    end_date=_as_date(s.end_date),
                    committed_points=int(v.committed_points),
                    completed_points=int(v.completed_points),
                    predictability=float(v.predictability),
                )
                for s, v in rows
            ]
            history = [
                VelocityResult(
                    committed_points=int(v.committed_points),
                    completed_points=int(v.completed_points),
                    added_points=int(v.added_points),
                    removed_points=int(v.removed_points),
                    carryover_points=int(v.carryover_points),
                    predictability=float(v.predictability),
                    scope_change_ratio=float(v.scope_change_ratio),
                )
                for _s, v in rows
            ]
            return VelocityDashboardView(
                project_id=project_id,
                sprints=bars,
                summary=compute_velocity_summary(history),
            )

    def velocity_export_rows(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        dashboard = self.velocity_dashboard(
            workspace_id=workspace_id, project_id=project_id, last=26
        )
        return [
            {
                "sprint_id": str(b.sprint_id),
                "name": b.name,
                "end_date": b.end_date.isoformat() if b.end_date else "",
                "committed_points": b.committed_points,
                "completed_points": b.completed_points,
                "predictability": b.predictability,
            }
            for b in dashboard.sprints
        ]


__all__ = [
    "BurndownSeriesView",
    "EstimationScaleView",
    "InvalidSprintRequest",
    "SprintNotFound",
    "SprintReportTaskView",
    "SprintReportView",
    "SprintService",
    "SprintView",
    "VelocityDashboardView",
    "VelocitySprintBarView",
]
