"""DORA metrics: deploy frequency, lead time, change-failure rate, MTTR.

Computed from the F31 ``deployment`` FSM-run table — no new columns, purely an
aggregation over the existing deployment-gates seam (``forge_db.models.deployment``).

Conformance note: "lead time for changes" is properly commit-to-production; this
foundation's ``deployment`` row carries no commit-authored/merged timestamp
(only ``commit_sha``), so ``lead_time_seconds`` uses ``requested_at ->
finished_at`` (the promotion's own request-to-live duration) as the measurable
proxy — documented here rather than silently approximated.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["DeploymentLike", "DoraMetrics", "SqlDoraReader", "compute_dora_metrics"]

#: Deployment states counted as a completed (terminal) promotion attempt.
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "gate_rejected", "rolled_back", "cancelled"}
)
#: States counted as a change failure in the DORA change-failure-rate ratio.
_FAILURE_STATES: frozenset[str] = frozenset({"failed", "gate_rejected", "rolled_back"})


@runtime_checkable
class DeploymentLike(Protocol):
    """The subset of ``deployment`` columns the DORA aggregation reads.

    Members are read-only (properties) rather than settable attributes: the ORM
    ``deployment`` row types ``state``/``kind`` as ``StrEnum`` (a subclass of
    ``str``), and a settable ``str`` protocol attribute is invariant and would
    reject them. The aggregation only ever reads these fields, so a covariant
    read-only view is both correct and accepts the enum-typed columns.
    """

    @property
    def environment_name(self) -> str: ...
    @property
    def state(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def requested_at(self) -> datetime | None: ...
    @property
    def started_at(self) -> datetime | None: ...
    @property
    def finished_at(self) -> datetime | None: ...


class DoraMetrics(BaseModel):
    """The four DORA keys for a scoped, windowed set of deployments."""

    deployment_count: int = 0
    successful_count: int = 0
    deploy_frequency_per_day: float = 0.0
    lead_time_seconds: float | None = None
    change_failure_rate: float | None = None
    mttr_seconds: float | None = None


def _state_of(row: DeploymentLike) -> str:
    return row.state.value if hasattr(row.state, "value") else str(row.state)


def _kind_of(row: DeploymentLike) -> str:
    return row.kind.value if hasattr(row.kind, "value") else str(row.kind)


def _deploy_mttr(deployments: Sequence[DeploymentLike]) -> float | None:
    """Mean time from a failed deployment to the next successful one, per env."""
    by_env: dict[str, list[DeploymentLike]] = {}
    for row in deployments:
        if row.finished_at is None or _kind_of(row) == "rollback":
            continue
        by_env.setdefault(row.environment_name, []).append(row)

    recoveries: list[float] = []
    for rows in by_env.values():
        ordered = sorted(rows, key=lambda r: r.finished_at)  # type: ignore[arg-type,return-value]
        pending_failure_at: datetime | None = None
        for row in ordered:
            state = _state_of(row)
            if state in _FAILURE_STATES:
                pending_failure_at = row.finished_at
            elif state == "succeeded" and pending_failure_at is not None:
                recoveries.append((row.finished_at - pending_failure_at).total_seconds())  # type: ignore[operator]
                pending_failure_at = None
    return sum(recoveries) / len(recoveries) if recoveries else None


def compute_dora_metrics(
    deployments: Iterable[DeploymentLike], *, window_days: float
) -> DoraMetrics:
    """Aggregate the four DORA keys over ``deployments`` within ``window_days``.

    ``window_days`` must be > 0 (the caller's query window, e.g. ``(to -
    frm).days``); deploy frequency is undefined (returned as ``0.0``) otherwise.
    """
    rows = [r for r in deployments if _kind_of(r) != "rollback"]
    terminal = [r for r in rows if _state_of(r) in _TERMINAL_STATES]
    successful = [r for r in terminal if _state_of(r) == "succeeded"]
    failed = [r for r in terminal if _state_of(r) in _FAILURE_STATES]

    lead_times = [
        (r.finished_at - r.requested_at).total_seconds()
        for r in successful
        if r.requested_at is not None and r.finished_at is not None
    ]

    return DoraMetrics(
        deployment_count=len(rows),
        successful_count=len(successful),
        deploy_frequency_per_day=(len(successful) / window_days) if window_days > 0 else 0.0,
        lead_time_seconds=(sum(lead_times) / len(lead_times)) if lead_times else None,
        change_failure_rate=(len(failed) / len(terminal)) if terminal else None,
        mttr_seconds=_deploy_mttr(rows),
    )


class SqlDoraReader:
    """Workspace-scoped DORA rollup over the real ``deployment`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def dora_metrics(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID | None = None,
        frm: datetime | None = None,
        to: datetime | None = None,
    ) -> DoraMetrics:
        from sqlalchemy import select

        from forge_db.models.deployment import Deployment

        with self._session_factory() as session:
            stmt = select(Deployment).where(Deployment.workspace_id == workspace_id)
            if project_id is not None:
                stmt = stmt.where(Deployment.project_id == project_id)
            if frm is not None:
                stmt = stmt.where(Deployment.requested_at >= frm)
            if to is not None:
                stmt = stmt.where(Deployment.requested_at < to)
            deployments = list(session.scalars(stmt))

        if frm is not None and to is not None:
            window_days = (to - frm).total_seconds() / 86400
        else:
            window_days = 1
        return compute_dora_metrics(deployments, window_days=window_days or 1)
