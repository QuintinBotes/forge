"""Incident reliability aggregation: MTTA / MTTR / remediation accept rate.

Reads the timestamps F17 already persists on ``incident``
(``detected_at`` / ``acknowledged_at`` / ``resolved_at``) and the decision
``status`` on ``remediation_plan`` — no new columns, purely an aggregation over
the existing seam. Two layers, mirroring ``forge_obs.cost``:

* :func:`compute_incident_reliability` — a pure function over plain rows (unit
  tests + the hermetic in-memory path).
* :class:`SqlIncidentReliabilityReader` — the real ``incident`` /
  ``remediation_plan`` tables via ``forge_db`` (sync sessions).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

__all__ = [
    "IncidentLike",
    "IncidentReliabilityMetrics",
    "RemediationLike",
    "SqlIncidentReliabilityReader",
    "compute_incident_reliability",
]

#: Remediation-plan decisions that count toward the accept-rate denominator.
_DECIDED_STATUSES: frozenset[str] = frozenset({"approved", "rejected"})
_ACCEPTED_STATUSES: frozenset[str] = frozenset({"approved"})


@runtime_checkable
class IncidentLike(Protocol):
    """The subset of ``incident`` columns the aggregation reads."""

    detected_at: datetime | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None


@runtime_checkable
class RemediationLike(Protocol):
    """The subset of ``remediation_plan`` columns the aggregation reads."""

    status: str


class IncidentReliabilityMetrics(BaseModel):
    """MTTA/MTTR/remediation-accept-rate for a scoped set of incidents."""

    sample_size: int = 0
    mtta_seconds: float | None = None
    mttr_seconds: float | None = None
    remediation_total: int = 0
    remediation_accepted: int = 0
    remediation_accept_rate: float | None = None


def _mean_seconds(deltas: Sequence[float]) -> float | None:
    return sum(deltas) / len(deltas) if deltas else None


def compute_incident_reliability(
    incidents: Iterable[IncidentLike],
    remediation_plans: Iterable[RemediationLike] = (),
) -> IncidentReliabilityMetrics:
    """Aggregate MTTA/MTTR/remediation-accept-rate over the given rows.

    ``mtta`` is the mean ``acknowledged_at - detected_at``; ``mttr`` is the mean
    ``resolved_at - detected_at``; each is computed only over incidents carrying
    both endpoints (an in-flight incident with no ``resolved_at`` contributes to
    ``sample_size`` but not to ``mttr_seconds``). The accept rate is
    ``accepted / (approved + rejected)`` — a plan still ``proposed`` has no
    decision yet and is excluded from both sides of the ratio.
    """
    incidents = list(incidents)
    mtta_deltas: list[float] = []
    mttr_deltas: list[float] = []
    for incident in incidents:
        if incident.detected_at is not None and incident.acknowledged_at is not None:
            mtta_deltas.append((incident.acknowledged_at - incident.detected_at).total_seconds())
        if incident.detected_at is not None and incident.resolved_at is not None:
            mttr_deltas.append((incident.resolved_at - incident.detected_at).total_seconds())

    decided = [p for p in remediation_plans if p.status in _DECIDED_STATUSES]
    accepted = [p for p in decided if p.status in _ACCEPTED_STATUSES]
    accept_rate = len(accepted) / len(decided) if decided else None

    return IncidentReliabilityMetrics(
        sample_size=len(incidents),
        mtta_seconds=_mean_seconds(mtta_deltas),
        mttr_seconds=_mean_seconds(mttr_deltas),
        remediation_total=len(decided),
        remediation_accepted=len(accepted),
        remediation_accept_rate=accept_rate,
    )


class SqlIncidentReliabilityReader:
    """Workspace-scoped reliability rollup over ``incident``/``remediation_plan``."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def reliability(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID | None = None,
        frm: datetime | None = None,
        to: datetime | None = None,
    ) -> IncidentReliabilityMetrics:
        from sqlalchemy import select

        from forge_db.models.incidents import RemediationPlan
        from forge_db.models.planning import Incident

        with self._session_factory() as session:
            incident_stmt = select(Incident).where(Incident.workspace_id == workspace_id)
            if project_id is not None:
                incident_stmt = incident_stmt.where(Incident.project_id == project_id)
            if frm is not None:
                incident_stmt = incident_stmt.where(Incident.detected_at >= frm)
            if to is not None:
                incident_stmt = incident_stmt.where(Incident.detected_at < to)
            incidents = list(session.scalars(incident_stmt))

            incident_ids = [row.id for row in incidents]
            plans: list[RemediationPlan] = []
            if incident_ids:
                plan_stmt = select(RemediationPlan).where(
                    RemediationPlan.workspace_id == workspace_id,
                    RemediationPlan.incident_id.in_(incident_ids),
                )
                plans = list(session.scalars(plan_stmt))

        return compute_incident_reliability(incidents, plans)
