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

from forge_board.capacity import MemberAllocation
from forge_board.estimation import EstimateChange
from forge_board.goal_alignment import GoalAlignmentResult
from forge_board.portfolio import CFDPoint, CycleLeadTime, PortfolioVelocitySummary
from forge_board.sprint_service import (
    BurndownSeriesView,
    EstimationScaleView,
    SprintReportView,
    SprintView,
    VelocityDashboardView,
)
from forge_contracts.enums import CarryoverTarget

__all__ = [
    "BurndownSeriesView",
    "CFDResponse",
    "CapacityReportResponse",
    "CompleteSprintRequest",
    "CycleLeadTimeResponse",
    "EstimateChange",
    "EstimationScaleCreate",
    "EstimationScaleUpdate",
    "EstimationScaleView",
    "GoalAlignmentResponse",
    "MemberCapacityUpdate",
    "PortfolioVelocityResponse",
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
    # F40 PM depth: working-day/holiday calendar the burndown ideal-line reads.
    calendar_weekend_days: list[int] = []
    calendar_holidays: list[date] = []


class SprintUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    goal: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    capacity_points: int | None = Field(default=None, ge=0)
    calendar_weekend_days: list[int] | None = None
    calendar_holidays: list[date] | None = None


class CompleteSprintRequest(BaseModel):
    carryover: CarryoverTarget = CarryoverTarget.BACKLOG
    next_sprint_id: uuid.UUID | None = None


class RecomputeResponse(BaseModel):
    enqueued: bool
    velocity_version: int


class MemberCapacityUpdate(BaseModel):
    """Declare (or replace) a member's capacity for a sprint."""

    member_id: uuid.UUID
    capacity_points: float = Field(ge=0)


class CapacityReportResponse(BaseModel):
    sprint_id: uuid.UUID
    members: list[MemberAllocation] = []


class CFDResponse(BaseModel):
    project_id: uuid.UUID
    start: date
    end: date
    points: list[CFDPoint] = []


class CycleLeadTimeResponse(BaseModel):
    project_id: uuid.UUID
    tasks: list[CycleLeadTime] = []
    average_lead_time_days: float = 0.0
    average_cycle_time_days: float = 0.0


class PortfolioVelocityResponse(PortfolioVelocitySummary):
    pass


class GoalAlignmentResponse(GoalAlignmentResult):
    sprint_id: uuid.UUID


class EstimationScaleCreate(BaseModel):
    """Declare a project-scoped (or, with ``project_id=None``, workspace-wide
    default) estimation scale."""

    project_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=64)
    unit: str = "points"
    values: list[float] = []
    is_default: bool = False


class EstimationScaleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    unit: str | None = None
    values: list[float] | None = None
    is_default: bool | None = None
