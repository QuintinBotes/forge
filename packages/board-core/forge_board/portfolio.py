"""Portfolio rollups: Cumulative Flow Diagram, cycle/lead time, cross-project
velocity (F40 PM depth).

Pure aggregation over the append-only ``task_status_event`` log (+ each
project's F26 :class:`~forge_board.velocity.VelocitySummary`); the DB-backed
service assembles the inputs. No I/O, deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel

from forge_board.velocity import VelocitySummary


def _days(start: date, end: date) -> list[date]:
    if end < start:
        return [start]
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


class TaskStatusEventInput(BaseModel):
    """A mirror of one ``task_status_event`` row, ordered by ``changed_at``."""

    task_id: str
    to_status: str
    changed_at: datetime


class CFDPoint(BaseModel):
    """One calendar day's task count per status (stacked-area chart input)."""

    snapshot_date: date
    status_counts: dict[str, int] = {}


def compute_cfd(
    events: list[TaskStatusEventInput],
    start: date,
    end: date,
    initial_status: str = "backlog",
) -> list[CFDPoint]:
    """One :class:`CFDPoint` per calendar day: each task counted under its
    last-known status as of end-of-day, defaulting to ``initial_status`` before
    its first recorded transition (pure; a task with zero events is invisible —
    the service only feeds in tasks that existed in the window)."""
    by_task: dict[str, list[TaskStatusEventInput]] = {}
    for ev in events:
        by_task.setdefault(ev.task_id, []).append(ev)
    for lst in by_task.values():
        lst.sort(key=lambda e: e.changed_at)

    task_ids = sorted(by_task)
    points: list[CFDPoint] = []
    for day in _days(start, end):
        counts: dict[str, int] = {}
        for task_id in task_ids:
            status = initial_status
            for ev in by_task[task_id]:
                if ev.changed_at.date() > day:
                    break
                status = ev.to_status
            counts[status] = counts.get(status, 0) + 1
        points.append(CFDPoint(snapshot_date=day, status_counts=counts))
    return points


class CycleLeadTime(BaseModel):
    """A task's lead time (created->done) and cycle time (first in-progress->done)."""

    task_id: str
    lead_time_days: float | None = None
    cycle_time_days: float | None = None


def compute_cycle_lead_time(
    events_by_task: dict[str, list[TaskStatusEventInput]],
    created_at_by_task: dict[str, datetime],
    done_status: str = "done",
    in_progress_status: str = "in_progress",
) -> list[CycleLeadTime]:
    """One :class:`CycleLeadTime` per task with a recorded ``done`` transition
    (tasks not yet done have both figures ``None``)."""
    out: list[CycleLeadTime] = []
    for task_id, events in events_by_task.items():
        ordered = sorted(events, key=lambda e: e.changed_at)
        done_at = next((e.changed_at for e in ordered if e.to_status == done_status), None)
        first_in_progress = next(
            (e.changed_at for e in ordered if e.to_status == in_progress_status), None
        )
        created_at = created_at_by_task.get(task_id)
        lead = (
            round((done_at - created_at).total_seconds() / 86400, 2)
            if (done_at and created_at)
            else None
        )
        cycle = (
            round((done_at - first_in_progress).total_seconds() / 86400, 2)
            if (done_at and first_in_progress)
            else None
        )
        out.append(CycleLeadTime(task_id=task_id, lead_time_days=lead, cycle_time_days=cycle))
    return out


def average_cycle_lead_time(rows: list[CycleLeadTime]) -> tuple[float, float]:
    """(avg lead time, avg cycle time) over rows with a recorded value; 0.0 if none."""
    leads = [r.lead_time_days for r in rows if r.lead_time_days is not None]
    cycles = [r.cycle_time_days for r in rows if r.cycle_time_days is not None]
    avg_lead = round(sum(leads) / len(leads), 2) if leads else 0.0
    avg_cycle = round(sum(cycles) / len(cycles), 2) if cycles else 0.0
    return avg_lead, avg_cycle


class PortfolioVelocitySummary(BaseModel):
    """Combined throughput/predictability trend across a workspace's projects."""

    project_count: int = 0
    total_average_velocity: float = 0.0
    total_forecast_avg: float = 0.0
    weighted_predictability: float = 0.0
    per_project: dict[str, VelocitySummary] = {}


def compute_portfolio_velocity(
    per_project: dict[str, VelocitySummary],
) -> PortfolioVelocitySummary:
    """Aggregate each project's independent velocity summary into one portfolio
    view (pure; empty input -> all zeros)."""
    if not per_project:
        return PortfolioVelocitySummary()
    total_sprints = sum(s.sprint_count for s in per_project.values())
    total_avg_velocity = sum(s.average_velocity for s in per_project.values())
    total_forecast_avg = sum(s.forecast_avg for s in per_project.values())
    if total_sprints > 0:
        weighted_predictability = round(
            sum(s.predictability_avg * s.sprint_count for s in per_project.values())
            / total_sprints,
            4,
        )
    else:
        weighted_predictability = 0.0
    return PortfolioVelocitySummary(
        project_count=len(per_project),
        total_average_velocity=round(total_avg_velocity, 2),
        total_forecast_avg=round(total_forecast_avg, 2),
        weighted_predictability=weighted_predictability,
        per_project=per_project,
    )


__all__ = [
    "CFDPoint",
    "CycleLeadTime",
    "PortfolioVelocitySummary",
    "TaskStatusEventInput",
    "average_cycle_lead_time",
    "compute_cfd",
    "compute_cycle_lead_time",
    "compute_portfolio_velocity",
]
