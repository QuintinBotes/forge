"""Decision drift: what has changed since a decision rested on its facts.

Pure module — no I/O. Because facts are immutable, what was believed is
exactly the referenced rows; drift is each row's status now: ``unchanged``
while live, ``invalidated`` with its reason, or ``superseded`` naming the
successor fact.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from forge_world.ledger import FactData, InvalidatedReason

DriftStatus = Literal["unchanged", "invalidated", "superseded"]


@dataclass(frozen=True)
class DriftItem:
    """One rested-on fact's current standing."""

    fact_id: uuid.UUID
    status: DriftStatus
    reason: str | None = None
    superseded_by: uuid.UUID | None = None


@dataclass(frozen=True)
class DriftReport:
    """Every rested-on fact's standing, and whether any drifted."""

    items: tuple[DriftItem, ...]
    drifted: bool


def drift(
    fact_rows: Sequence[FactData],
    successors: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> DriftReport:
    """Report drift for ``fact_rows`` (successors keyed by superseded id)."""
    successor_of = successors or {}
    items: list[DriftItem] = []
    for fact in fact_rows:
        if fact.invalidated_at is None:
            items.append(DriftItem(fact_id=fact.id, status="unchanged"))
        elif fact.invalidated_reason is InvalidatedReason.SUPERSEDED:
            items.append(
                DriftItem(
                    fact_id=fact.id,
                    status="superseded",
                    superseded_by=successor_of.get(fact.id),
                )
            )
        else:
            items.append(
                DriftItem(
                    fact_id=fact.id,
                    status="invalidated",
                    reason=(
                        fact.invalidated_reason.value
                        if fact.invalidated_reason is not None
                        else None
                    ),
                )
            )
    return DriftReport(
        items=tuple(items), drifted=any(item.status != "unchanged" for item in items)
    )


__all__ = ["DriftItem", "DriftReport", "DriftStatus", "drift"]
