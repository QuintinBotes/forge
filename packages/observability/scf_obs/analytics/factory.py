"""The five factory metrics (spec 005 FR-002 to FR-010, #196).

:func:`compute_factory_metrics` is the written formula of
``docs/metrics/factory-metrics.md`` over in-memory :class:`Attempt` records and
nothing else. :class:`SqlFactoryMetricsReader` builds those records for one
workspace from ``agent_run``, ``change_outcome``, ``run_intervention``,
``approval_request``, ``sub_agent_run`` and ``cost_event``.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "Attempt",
    "CostMetric",
    "CycleTimeMetric",
    "FactoryMetric",
    "FactoryMetricsReport",
    "RevertMetric",
    "SqlFactoryMetricsReader",
    "compute_factory_metrics",
]

#: How long after landing a revert still counts (FR-005), and the provisional age.
OBSERVATION_DAYS = 30
#: The window a request without ``since`` covers, ending at ``until``.
DEFAULT_WINDOW_DAYS = 30

LANDED = "landed"
#: ``agent_run.role`` of a run that builds a change (the column's default).
PRIMARY_ROLE = "primary"
#: The intervention kinds of FR-006.
GATE_DECISION = "gate_decision"
REVIEW_CHANGES_REQUESTED = "review_changes_requested"
FOREIGN_COMMIT = "foreign_commit"
ESCALATED = "escalated"

NO_SETTLED = "no settled attempt in the window"
NO_LANDED = "no landed change in the window"


@dataclass(frozen=True)
class Attempt:
    """One top-level agent run and what it caused (FR-002)."""

    run_id: UUID
    created_at: datetime
    #: When the run reached a terminal status; ``None`` while it runs.
    ended_at: datetime | None
    #: It opened a pull request.
    produced_change: bool
    #: ``landed`` | ``closed`` once the pull request has a final outcome.
    outcome: str | None
    outcome_at: datetime | None
    reverted_at: datetime | None = None
    #: One kind per intervention.
    interventions: tuple[str, ...] = ()
    #: The run's priced cost plus its sub-agent runs'.
    cost_usd: Decimal = Decimal(0)
    cost_events: int = 0
    unpriced_cost_events: int = 0

    @property
    def settled_at(self) -> datetime | None:
        """Terminal, and its change (if any) has a final outcome; else ``None``."""
        if self.ended_at is None:
            return None
        if not self.produced_change:
            return self.ended_at
        if self.outcome_at is None:
            return None
        return max(self.ended_at, self.outcome_at)


class FactoryMetric(BaseModel):
    """One metric with what it was computed from (FR-008)."""

    value: float | None
    numerator: float | None
    denominator: int
    sample_size: int
    available: bool
    #: Why the value is not available.
    reason: str | None = None


class CostMetric(FactoryMetric):
    """Cost per merged change, in USD, with the cost events it summed."""

    cost_events: int = 0
    #: Events with no price: counted here, their cost is unknown, never dropped.
    unpriced_cost_events: int = 0


class RevertMetric(FactoryMetric):
    observation_days: int = OBSERVATION_DAYS
    #: The window's newest landed change is younger than the observation period.
    provisional: bool = False


class CycleTimeMetric(FactoryMetric):
    """Creation to landing over landed attempts; ``value`` is the median."""

    median_seconds: float | None = None
    p90_seconds: float | None = None


class FactoryMetricsReport(BaseModel):
    since: datetime
    until: datetime
    repository: str | None = None
    #: ``api`` | ``tracker`` when the report is filtered by how attempts started.
    source: str | None = None
    autonomous_merge_rate: FactoryMetric
    cost_per_merged_change: CostMetric
    revert_rate: RevertMetric
    human_intervention_rate: FactoryMetric
    cycle_time: CycleTimeMetric


def _ratio(numerator: int, denominator: int, *, sample: int, reason: str) -> dict[str, Any]:
    available = denominator > 0
    return {
        "value": numerator / denominator if available else None,
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": sample,
        "available": available,
        "reason": None if available else reason,
    }


def compute_factory_metrics(
    attempts: Iterable[Attempt],
    *,
    since: datetime,
    until: datetime,
    now: datetime,
    observation_days: int = OBSERVATION_DAYS,
) -> FactoryMetricsReport:
    """The five metrics over the attempts that settled in ``[since, until)``."""
    settled = [a for a in attempts if (t := a.settled_at) is not None and since <= t < until]
    landed = [
        (a, a.outcome_at) for a in settled if a.outcome == LANDED and a.outcome_at is not None
    ]
    period = timedelta(days=observation_days)

    autonomous = sum(1 for a, _ in landed if not a.interventions)
    intervened = sum(1 for a in settled if a.interventions)
    reverted = sum(
        1 for a, at in landed if a.reverted_at is not None and a.reverted_at - at <= period
    )
    total_cost = sum((a.cost_usd for a in settled), Decimal(0))
    cycle = [(at - a.created_at).total_seconds() for a, at in landed]
    newest = max((at for _, at in landed), default=None)

    cost = _ratio(0, len(landed), sample=len(settled), reason=NO_LANDED)
    cost |= {
        "numerator": float(total_cost),
        "value": float(total_cost / len(landed)) if landed else None,
    }
    median = statistics.median(cycle) if cycle else None
    p90 = _p90(cycle) if cycle else None
    return FactoryMetricsReport(
        since=since,
        until=until,
        autonomous_merge_rate=FactoryMetric(
            **_ratio(autonomous, len(settled), sample=len(settled), reason=NO_SETTLED)
        ),
        cost_per_merged_change=CostMetric(
            **cost,
            cost_events=sum(a.cost_events for a in settled),
            unpriced_cost_events=sum(a.unpriced_cost_events for a in settled),
        ),
        revert_rate=RevertMetric(
            **_ratio(reverted, len(landed), sample=len(landed), reason=NO_LANDED),
            observation_days=observation_days,
            provisional=newest is not None and now - newest < period,
        ),
        human_intervention_rate=FactoryMetric(
            **_ratio(intervened, len(settled), sample=len(settled), reason=NO_SETTLED)
        ),
        cycle_time=CycleTimeMetric(
            **_ratio(0, len(landed), sample=len(landed), reason=NO_LANDED)
            | {"value": median, "numerator": None},
            median_seconds=median,
            p90_seconds=p90,
        ),
    )


def _p90(values: Sequence[float]) -> float:
    """The 90th percentile, linear between closest ranks (``method="inclusive"``)."""
    return statistics.quantiles(values, n=10, method="inclusive")[-1]


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; every stored time is UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SqlFactoryMetricsReader:
    """Workspace-scoped factory metrics over the real tables (FR-009, FR-010)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def report(
        self,
        *,
        workspace_id: UUID,
        since: datetime | None = None,
        until: datetime | None = None,
        repository: str | None = None,
        source: str | None = None,
        now: datetime | None = None,
    ) -> FactoryMetricsReport:
        now = _utc(now) if now is not None else datetime.now(UTC)
        until = _utc(until) if until is not None else now
        since = _utc(since) if since is not None else until - timedelta(days=DEFAULT_WINDOW_DAYS)
        attempts = self.attempts(
            workspace_id=workspace_id,
            since=since,
            until=until,
            repository=repository,
            source=source,
        )
        report = compute_factory_metrics(attempts, since=since, until=until, now=now)
        return report.model_copy(update={"repository": repository, "source": source})

    def attempts(
        self,
        *,
        workspace_id: UUID,
        since: datetime,
        until: datetime,
        repository: str | None = None,
        source: str | None = None,
    ) -> list[Attempt]:
        """Top-level runs that may have settled in the window, with their evidence."""
        from sqlalchemy import func, or_, select

        from scf_db.models import (
            AgentRun,
            ApprovalRequest,
            ApprovalStatus,
            ChangeOutcome,
            CostEvent,
            RunIntervention,
            RunStatus,
            SubAgentRun,
            WorkflowRun,
        )

        done = [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED]
        # A run inside a workflow run ends when its workflow run does: the workflow,
        # not the run, opens the pull request.
        ended = func.coalesce(
            WorkflowRun.completed_at,
            WorkflowRun.updated_at,
            AgentRun.completed_at,
            AgentRun.updated_at,
        )
        with self._session_factory() as session:
            parent_of: dict[UUID, UUID] = {
                child: parent
                for child, parent in session.execute(
                    select(SubAgentRun.agent_run_id, SubAgentRun.parent_agent_run_id).where(
                        SubAgentRun.workspace_id == workspace_id,
                        SubAgentRun.agent_run_id.is_not(None),
                    )
                )
                if child is not None
            }
            # settled_at = max(ended, outcome_at): in the window only if both are
            # before ``until`` and either is at or after ``since``.
            # shortcut: the repository and source filters run in Python over this
            # window's runs; push them into SQL if a window grows past a few thousand.
            rows = [
                (run, outcome, flow)
                for run, outcome, flow in session.execute(
                    select(AgentRun, ChangeOutcome, WorkflowRun)
                    .outerjoin(ChangeOutcome, ChangeOutcome.agent_run_id == AgentRun.id)
                    .outerjoin(WorkflowRun, WorkflowRun.id == AgentRun.workflow_run_id)
                    .where(
                        AgentRun.workspace_id == workspace_id,
                        # A governed run's verifier and reviewer runs are checks, not attempts.
                        AgentRun.role == PRIMARY_ROLE,
                        AgentRun.status.in_([*done, RunStatus.ESCALATED]),
                        or_(WorkflowRun.id.is_(None), WorkflowRun.status.in_(done)),
                        ended < until,
                        or_(ChangeOutcome.outcome_at.is_(None), ChangeOutcome.outcome_at < until),
                        or_(ended >= since, ChangeOutcome.outcome_at >= since),
                    )
                )
                if run.id not in parent_of
                and _matches(run.inputs, outcome, repository=repository, source=source)
            ]
            ids = {run.id for run, _, _ in rows}
            # The workflow delivers its newest builder run's branch (``_latest_agent_run``).
            newest = dict(
                session.execute(
                    select(AgentRun.workflow_run_id, func.max(AgentRun.created_at))
                    .where(
                        AgentRun.workflow_run_id.in_({f.id for _, _, f in rows if f is not None}),
                        AgentRun.role == PRIMARY_ROLE,
                    )
                    .group_by(AgentRun.workflow_run_id)
                ).all()
            )

            def root(run_id: UUID) -> UUID:
                for _ in range(len(parent_of) + 1):  # bounded: a cycle cannot loop forever
                    if run_id not in parent_of:
                        break
                    run_id = parent_of[run_id]
                return run_id

            members = ids | {child for child in parent_of if root(child) in ids}
            cost: dict[UUID, list[Any]] = defaultdict(lambda: [Decimal(0), 0, 0])
            for run_id, total, events, priced in session.execute(
                select(
                    CostEvent.agent_run_id,
                    func.sum(CostEvent.cost_usd),
                    func.count(CostEvent.id),
                    func.count(CostEvent.price_id),
                )
                .where(CostEvent.workspace_id == workspace_id, CostEvent.agent_run_id.in_(members))
                .group_by(CostEvent.agent_run_id)
            ):
                entry = cost[root(run_id)]  # type: ignore[arg-type]
                entry[0] += Decimal(str(total or 0))
                entry[1] += events
                entry[2] += events - priced

            kinds: dict[UUID, list[str]] = defaultdict(list)
            for run_id, kind in session.execute(
                select(RunIntervention.agent_run_id, RunIntervention.kind).where(
                    RunIntervention.workspace_id == workspace_id,
                    RunIntervention.agent_run_id.in_(members),
                )
            ):
                kinds[root(run_id)].append(kind)

            by_workflow: dict[UUID, list[UUID]] = defaultdict(list)
            for run, _, _ in rows:
                if run.workflow_run_id is not None:
                    by_workflow[run.workflow_run_id].append(run.id)
            for gate_run_id, gate_workflow_id in session.execute(
                select(ApprovalRequest.agent_run_id, ApprovalRequest.workflow_run_id).where(
                    ApprovalRequest.workspace_id == workspace_id,
                    ApprovalRequest.status.in_(
                        [ApprovalStatus.REJECTED, ApprovalStatus.CHANGES_REQUESTED]
                    ),
                    or_(
                        ApprovalRequest.agent_run_id.in_(members),
                        ApprovalRequest.workflow_run_id.in_(list(by_workflow)),
                    ),
                )
            ):
                if gate_run_id is not None and gate_run_id in members:
                    targets = [root(gate_run_id)]
                else:
                    targets = by_workflow.get(gate_workflow_id, [])  # type: ignore[arg-type]
                for target in targets:
                    kinds[target].append(GATE_DECISION)

        attempts = []
        for run, outcome, flow in rows:
            spent, events, unpriced = cost.get(run.id, (Decimal(0), 0, 0))
            run_kinds = kinds.get(run.id, [])
            if run.status == RunStatus.ESCALATED:
                run_kinds = [*run_kinds, ESCALATED]
            end: Any = flow if flow is not None else run
            reverted = outcome.reverted_at if outcome is not None else None
            attempts.append(
                Attempt(
                    run_id=run.id,
                    created_at=_utc(run.created_at),
                    ended_at=_utc(end.completed_at or end.updated_at),
                    produced_change=outcome is not None
                    or _has_pr((run.output or {}).get("artifacts"))
                    or (
                        flow is not None
                        and _has_pr(flow.context)
                        and newest.get(flow.id) == run.created_at
                    ),
                    outcome=outcome.outcome if outcome is not None else None,
                    outcome_at=_utc(outcome.outcome_at) if outcome is not None else None,
                    reverted_at=_utc(reverted) if reverted is not None else None,
                    interventions=tuple(run_kinds),
                    cost_usd=spent,
                    cost_events=events,
                    unpriced_cost_events=unpriced,
                )
            )
        return attempts


def _context(inputs: Any) -> dict[str, Any]:
    context = inputs.get("context") if isinstance(inputs, dict) else None
    return context if isinstance(context, dict) else {}


def _has_pr(holder: Any) -> bool:
    """A run's artifacts or a workflow run's context name an opened pull request."""
    pull_request = holder.get("pull_request") if isinstance(holder, dict) else None
    return isinstance(pull_request, dict) and pull_request.get("number") is not None


def _matches(inputs: Any, outcome: Any, *, repository: str | None, source: str | None) -> bool:
    """FR-010: the attempt's repository and how it started (``api`` or ``tracker``)."""
    context = _context(inputs)
    if repository is not None:
        repo = outcome.repo_full_name if outcome is not None else context.get("repo_full_name")
        if not isinstance(repo, str) or repo.casefold() != repository.casefold():
            return False
    # A tracker dispatch carries its tracker reference as the server-set source.
    started = "tracker" if context.get("source") else "api"
    return source is None or source == started
