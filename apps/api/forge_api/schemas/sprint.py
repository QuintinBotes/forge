"""Request/response schemas for the F26 sprint + velocity router.

Response models reuse the ``forge_board`` view models (``SprintView`` etc.) — the
service already returns plain Pydantic transport objects, so the router uses them
directly as ``response_model`` rather than duplicating the shape here. This module
defines the request bodies + a couple of thin response wrappers.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field

from forge_board.sprint_service import (
    BurndownSeriesView,
    SprintReportView,
    SprintView,
    VelocityDashboardView,
)
from forge_contracts.enums import CarryoverTarget

__all__ = [
    "BurndownSeriesView",
    "CompleteSprintRequest",
    "RecomputeResponse",
    "SprintCreate",
    "SprintReportView",
    "SprintUpdate",
    "SprintView",
    "VelocityDashboardView",
]


class SprintCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    goal: str | None = None
    start_date: date
    end_date: date
    capacity_points: int | None = Field(default=None, ge=0)


class SprintUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    goal: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    capacity_points: int | None = Field(default=None, ge=0)


class CompleteSprintRequest(BaseModel):
    carryover: CarryoverTarget = CarryoverTarget.BACKLOG
    next_sprint_id: uuid.UUID | None = None


class RecomputeResponse(BaseModel):
    enqueued: bool
    velocity_version: int
