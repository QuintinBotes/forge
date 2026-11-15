"""Pure per-member sprint capacity math (F40 PM depth).

No I/O: computes over/under/balanced allocation from a member's declared
sprint capacity and the sum of estimate points on tasks assigned to them
within the sprint. The DB-backed service (``sprint_service.capacity_report``)
assembles the inputs from ``sprint_member_capacity`` + ``task`` rows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

AllocationStatus = Literal["under", "balanced", "over"]

_BALANCE_BAND = 0.1


class MemberCapacityInput(BaseModel):
    """A member's declared capacity for one sprint."""

    member_id: str
    capacity_points: float = 0.0


class MemberAssignment(BaseModel):
    """A single task's point contribution toward a member's sprint load."""

    member_id: str
    points: int = 0


class MemberAllocation(BaseModel):
    """One member's capacity-vs-assigned rollup for a sprint."""

    member_id: str
    capacity_points: float = 0.0
    assigned_points: int = 0
    utilization: float = 0.0
    status: AllocationStatus = "under"


def compute_capacity_report(
    capacities: list[MemberCapacityInput], assignments: list[MemberAssignment]
) -> list[MemberAllocation]:
    """One :class:`MemberAllocation` per member with a declared capacity or an
    assignment (pure; a member with neither never appears)."""
    assigned_by_member: dict[str, int] = {}
    for a in assignments:
        assigned_by_member[a.member_id] = assigned_by_member.get(a.member_id, 0) + a.points

    capacity_by_member: dict[str, float] = {c.member_id: c.capacity_points for c in capacities}

    member_ids = list(dict.fromkeys([*capacity_by_member, *assigned_by_member]))

    out: list[MemberAllocation] = []
    for member_id in member_ids:
        capacity = capacity_by_member.get(member_id, 0.0)
        assigned = assigned_by_member.get(member_id, 0)
        if capacity > 0:
            utilization = round(assigned / capacity, 4)
        else:
            utilization = 1.0 if assigned > 0 else 0.0
        status: AllocationStatus
        if utilization > 1 + _BALANCE_BAND:
            status = "over"
        elif utilization < 1 - _BALANCE_BAND:
            status = "under"
        else:
            status = "balanced"
        out.append(
            MemberAllocation(
                member_id=member_id,
                capacity_points=capacity,
                assigned_points=assigned,
                utilization=utilization,
                status=status,
            )
        )
    return out


__all__ = [
    "AllocationStatus",
    "MemberAllocation",
    "MemberAssignment",
    "MemberCapacityInput",
    "compute_capacity_report",
]
