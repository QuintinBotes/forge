"""Configurable estimation scales (F40 PM depth).

Pure validation/snap helpers over a project's (or workspace's) declared
estimation scale — Fibonacci story points, ideal days, a numeric T-shirt
mapping, whatever the team wants. The DB-backed ``estimation_scale`` table
stores the raw ``values``; this module is the shared math the service and any
client-side picker both call. No I/O.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class EstimationScale(BaseModel):
    """A named, ordered set of allowed estimate values."""

    name: str
    unit: str = "points"
    values: list[float] = []
    is_default: bool = False


def is_valid_estimate(scale: EstimationScale, value: float) -> bool:
    """True iff ``value`` is one of the scale's declared values.

    An empty scale (no ``values`` configured) allows anything — estimation
    scales are opt-in, so a project with none configured is unrestricted.
    """
    if not scale.values:
        return True
    return any(abs(value - v) < 1e-9 for v in scale.values)


def nearest_scale_value(scale: EstimationScale, value: float) -> float:
    """Snap ``value`` to the closest declared scale value (identity if empty)."""
    if not scale.values:
        return value
    return min(scale.values, key=lambda v: abs(v - value))


class EstimateChange(BaseModel):
    """One entry in a task's estimate-change history (mirrors ``task_estimate_event``).

    Recorded on every estimate edit regardless of sprint state — unlike the
    F26 ``ESTIMATE_CHANGED`` scope event, which only fires while the task's
    sprint is active. This is the durable, always-on history a burn-up review
    or an estimation-accuracy report reads from.
    """

    task_id: str
    points_before: int | None = None
    points_after: int | None = None
    changed_at: datetime
    actor_id: str | None = None


__all__ = ["EstimateChange", "EstimationScale", "is_valid_estimate", "nearest_scale_value"]
