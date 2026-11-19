"""The five factory metrics (spec 005 FR-002 to FR-008, #196): the pure formulas."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from scf_db.base import Base
from scf_db.models import (
    AgentRun,
    ApprovalGate,
    ApprovalRequest,
    ApprovalStatus,
    ChangeOutcome,
    CostEvent,
    CostEventKind,
    ModelPrice,
    Project,
    RunStatus,
    SubAgentRun,
    Task,
    WorkflowRun,
    Workspace,
)
from scf_obs.analytics.factory import (
    Attempt,
    FactoryMetricsReport,
    SqlFactoryMetricsReader,
    compute_factory_metrics,
)

SINCE = datetime(2026, 6, 1, tzinfo=UTC)
UNTIL = datetime(2026, 7, 1, tzinfo=UTC)
NOW = datetime(2026, 9, 1, tzinfo=UTC)
T0 = datetime(2026, 6, 10, tzinfo=UTC)


def _attempt(
    *,
    landed_after_hours: float | None = None,
    closed: bool = False,
    interventions: tuple[str, ...] = (),
    cost: str = "0",
    reverted_after_days: float | None = None,
    created_at: datetime = T0,
) -> Attempt:
    ended = created_at + timedelta(minutes=30)
    outcome_at: datetime | None = None
    outcome: str | None = None
    if landed_after_hours is not None:
        outcome, outcome_at = "landed", created_at + timedelta(hours=landed_after_hours)
    elif closed:
        outcome, outcome_at = "closed", created_at + timedelta(hours=1)
    reverted_at = (
        outcome_at + timedelta(days=reverted_after_days)
        if outcome_at is not None and reverted_after_days is not None
        else None
    )
    return Attempt(
        run_id=uuid.uuid4(),
        created_at=created_at,
        ended_at=ended,
        produced_change=outcome is not None,
        outcome=outcome,
        outcome_at=outcome_at,
        reverted_at=reverted_at,
        interventions=interventions,
        cost_usd=Decimal(cost),
    )


def _fixture() -> list[Attempt]:
    """10 settled attempts: 6 landed (4 with no intervention, 1 reverted), 4 not."""
    return [
        _attempt(landed_after_hours=1, cost="1.10"),
        _attempt(landed_after_hours=2, cost="2.20", reverted_after_days=3),
        _attempt(landed_after_hours=3, cost="0.30"),
        _attempt(landed_after_hours=4, cost="0.40"),
        _attempt(landed_after_hours=5, cost="0.50", interventions=("review_changes_requested",)),
        _attempt(landed_after_hours=10, cost="1.00", interventions=("foreign_commit",)),
        _attempt(closed=True, cost="0.70", interventions=("gate_decision",)),
        _attempt(closed=True, cost="0.80"),
        # A run with no pull request settles when it ends; its cost still counts.
        _attempt(cost="0.90", interventions=("escalated",)),
        _attempt(cost="0.10"),
    ]


def test_the_ten_attempt_fixture_matches_the_written_formulas() -> None:
    report = compute_factory_metrics(_fixture(), since=SINCE, until=UNTIL, now=NOW)

    amr = report.autonomous_merge_rate
    assert (amr.value, amr.numerator, amr.denominator, amr.sample_size) == (0.4, 4, 10, 10)

    hir = report.human_intervention_rate
    assert (hir.value, hir.numerator, hir.denominator) == (pytest.approx(0.4), 4, 10)

    rr = report.revert_rate
    assert rr.value == pytest.approx(1 / 6)
    assert (rr.numerator, rr.denominator, rr.sample_size) == (1, 6, 6)
    assert rr.provisional is False

    total = Decimal("1.10") + Decimal("2.20") + Decimal("0.30") + Decimal("0.40")
    total += Decimal("0.50") + Decimal("1.00") + Decimal("0.70") + Decimal("0.80")
    total += Decimal("0.90") + Decimal("0.10")
    cpm = report.cost_per_merged_change
    assert cpm.value == pytest.approx(float(total / 6))
    assert cpm.numerator == pytest.approx(float(total))
    assert (cpm.denominator, cpm.sample_size) == (6, 10)

    ct = report.cycle_time
    # Landed after 1, 2, 3, 4, 5 and 10 hours: median 3.5 h, p90 interpolated 7.5 h.
    assert ct.median_seconds == pytest.approx(3.5 * 3600)
    assert ct.p90_seconds == pytest.approx(7.5 * 3600)
    assert ct.value == ct.median_seconds
    assert (ct.denominator, ct.sample_size) == (6, 6)

    assert (report.since, report.until) == (SINCE, UNTIL)


def test_attempts_are_selected_by_settle_time_and_unsettled_ones_are_left_out() -> None:
    before = _attempt(landed_after_hours=1, created_at=SINCE - timedelta(days=3))
    # Created before the window but landed inside it: it settles in the window.
    landed_in_window = _attempt(landed_after_hours=72, created_at=SINCE - timedelta(days=2))
    open_pr = replace(_attempt(), produced_change=True)
    running = replace(_attempt(), ended_at=None)
    report = compute_factory_metrics(
        [before, landed_in_window, open_pr, running], since=SINCE, until=UNTIL, now=NOW
    )
    assert report.autonomous_merge_rate.denominator == 1
    assert report.autonomous_merge_rate.numerator == 1


def test_no_landed_change_reports_not_available_with_a_reason_never_zero() -> None:
    attempts = [_attempt(closed=True, cost="2.00"), _attempt(cost="1.00")]
    report = compute_factory_metrics(attempts, since=SINCE, until=UNTIL, now=NOW)

    for metric in (report.cost_per_merged_change, report.revert_rate, report.cycle_time):
        assert metric.available is False
        assert metric.value is None
        assert metric.reason == "no landed change in the window"
        assert metric.denominator == 0
    assert report.cycle_time.median_seconds is None
    assert report.cycle_time.p90_seconds is None
    # The spend is still reported: it is what the window cost for nothing landed.
    assert report.cost_per_merged_change.numerator == pytest.approx(3.0)
    # The rates over settled attempts stay defined.
    assert report.autonomous_merge_rate.value == 0.0
    assert report.autonomous_merge_rate.available is True


def test_an_empty_window_reports_every_metric_not_available() -> None:
    report = compute_factory_metrics([], since=SINCE, until=UNTIL, now=NOW)
    assert report.autonomous_merge_rate.available is False
    assert report.autonomous_merge_rate.reason == "no settled attempt in the window"
    assert report.human_intervention_rate.value is None


def test_a_revert_counts_only_within_the_observation_period_of_landing() -> None:
    late = _attempt(landed_after_hours=1, reverted_after_days=31)
    report = compute_factory_metrics([late], since=SINCE, until=UNTIL, now=NOW)
    assert report.revert_rate.numerator == 0


def test_a_window_whose_newest_landing_is_younger_than_the_period_is_provisional() -> None:
    attempts = _fixture()
    newest = max(a.outcome_at for a in attempts if a.outcome_at is not None)
    young = compute_factory_metrics(
        attempts, since=SINCE, until=UNTIL, now=newest + timedelta(days=29)
    )
    assert young.revert_rate.provisional is True
    settled = compute_factory_metrics(
        attempts, since=SINCE, until=UNTIL, now=newest + timedelta(days=30)
    )
    assert settled.revert_rate.provisional is False
    assert settled.revert_rate.observation_days == 30


def test_unpriced_cost_events_are_summed_into_the_report() -> None:
    attempts = [replace(_attempt(cost="1.5"), cost_events=3, unpriced_cost_events=2)]
    report = compute_factory_metrics(attempts, since=SINCE, until=UNTIL, now=NOW)
    assert report.cost_per_merged_change.cost_events == 3
    assert report.cost_per_merged_change.unpriced_cost_events == 2


# --------------------------------------------------------------------------- #
# SqlFactoryMetricsReader: the evidence rows, on SQLite                        #
# --------------------------------------------------------------------------- #

WS = uuid.UUID("00000000-0000-0000-0000-0000000196a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000196a2")


@pytest.fixture(params=["sqlite", "postgres"])
def factory(request: pytest.FixtureRequest) -> Iterator[sessionmaker[Session]]:
    """The reader on SQLite always, and on Postgres when one is configured."""
    if request.param == "postgres":
        engine = request.getfixturevalue("pg_engine")
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add_all(
            [Workspace(id=WS, name="A", slug="a"), Workspace(id=WS2, name="B", slug="b")]
        )
        session.commit()
    yield sf
    if request.param == "postgres":
        Base.metadata.drop_all(engine)
    else:
        engine.dispose()


def _run(
    session: Session,
    *,
    workspace_id: uuid.UUID = WS,
    status: RunStatus = RunStatus.SUCCEEDED,
    landed: bool | None = None,
    repo: str = "org/api",
    source: str | None = None,
    workflow_run_id: uuid.UUID | None = None,
) -> AgentRun:
    context: dict[str, str] = {"repo_full_name": repo}
    if source:
        context["source"] = source
    run = AgentRun(
        workspace_id=workspace_id,
        status=status,
        inputs={"context": context},
        workflow_run_id=workflow_run_id,
        created_at=T0,
        completed_at=T0 + timedelta(hours=1),
    )
    session.add(run)
    session.flush()
    if landed is not None:
        session.add(
            ChangeOutcome(
                workspace_id=workspace_id,
                agent_run_id=run.id,
                repo_full_name=repo,
                pr_number=run.id.int % 10_000,
                outcome="landed" if landed else "closed",
                outcome_at=T0 + timedelta(hours=2),
            )
        )
    return run


def _cost(
    session: Session,
    run: AgentRun,
    usd: str,
    *,
    price: ModelPrice | None,
    workspace_id: uuid.UUID = WS,
) -> None:
    session.add(
        CostEvent(
            workspace_id=workspace_id,
            agent_run_id=run.id,
            kind=CostEventKind.COMPLETION,
            provider="anthropic",
            model="m",
            cost_usd=Decimal(usd),
            price_id=price.id if price is not None else None,
            request_id=uuid.uuid4().hex,
            occurred_at=T0,
        )
    )


def _report(factory: sessionmaker[Session], **filters: str) -> FactoryMetricsReport:
    return SqlFactoryMetricsReader(factory).report(
        workspace_id=WS, since=SINCE, until=UNTIL, now=NOW, **filters
    )


@pytest.mark.filterwarnings("error::DeprecationWarning")  # the reader uses no deprecated API
def test_sub_agent_runs_add_cost_to_their_parent_and_are_never_attempts(factory) -> None:
    with factory() as session:
        price = ModelPrice(provider="anthropic", model="m", effective_from=T0)
        session.add(price)
        parent = _run(session, landed=True)
        child = _run(session)
        session.add(
            SubAgentRun(
                workspace_id=WS, parent_agent_run_id=parent.id, agent_run_id=child.id, role="coder"
            )
        )
        session.flush()
        _cost(session, parent, "1.25", price=price)
        _cost(session, child, "0.75", price=price)
        # An unpriced call is counted and reported, not dropped.
        _cost(session, child, "0", price=None)
        # A governed run's verifier and reviewer runs check the work; they are not attempts.
        for role in ("verifier", "reviewer"):
            _run(session).role = role
        session.commit()

    report = _report(factory)
    assert report.autonomous_merge_rate.denominator == 1
    cost = report.cost_per_merged_change
    assert cost.value == pytest.approx(2.0)
    assert (cost.cost_events, cost.unpriced_cost_events) == (3, 1)


def test_gate_rejections_and_escalations_are_interventions_and_approvals_are_not(
    factory,
) -> None:
    with factory() as session:
        project = Project(workspace_id=WS, name="P", key="P196")
        session.add(project)
        session.flush()
        task = Task(workspace_id=WS, project_id=project.id, key="P-1", title="T")
        session.add(task)
        session.flush()
        workflow = WorkflowRun(
            workspace_id=WS, task_id=task.id, status=RunStatus.SUCCEEDED, completed_at=T0
        )
        session.add(workflow)
        session.flush()
        rejected = _run(session, landed=True)
        approved = _run(session, landed=True)
        escalated = _run(session, status=RunStatus.ESCALATED)
        governed = _run(session, landed=True, workflow_run_id=workflow.id)
        for run, status in (
            (rejected, ApprovalStatus.REJECTED),
            (approved, ApprovalStatus.APPROVED),
        ):
            session.add(
                ApprovalRequest(
                    workspace_id=WS, agent_run_id=run.id, gate=ApprovalGate.PR, status=status
                )
            )
        # A governed run's gate is on its workflow run, not on the agent run.
        session.add(
            ApprovalRequest(
                workspace_id=WS,
                workflow_run_id=workflow.id,
                gate=ApprovalGate.SPEC,
                status=ApprovalStatus.CHANGES_REQUESTED,
            )
        )
        session.commit()

    reader = SqlFactoryMetricsReader(factory)
    attempts = {
        a.run_id: a.interventions
        for a in reader.attempts(workspace_id=WS, since=SINCE, until=UNTIL)
    }
    assert attempts == {
        rejected.id: ("gate_decision",),
        approved.id: (),
        escalated.id: ("escalated",),
        governed.id: ("gate_decision",),
    }
    report = _report(factory)
    assert report.autonomous_merge_rate.numerator == 1
    assert report.human_intervention_rate.numerator == 3


def test_reports_never_include_another_workspace_and_filter_by_repo_and_source(
    factory,
) -> None:
    with factory() as session:
        _run(session, landed=True)
        _run(session, landed=False, repo="org/web", source="github_issues:org/web#4")
        foreign = _run(session, workspace_id=WS2, landed=True)
        _cost(session, foreign, "9", price=None, workspace_id=WS2)
        session.commit()

    assert _report(factory).autonomous_merge_rate.denominator == 2
    assert _report(factory).cost_per_merged_change.cost_events == 0
    by_repo = _report(factory, repository="org/web")
    assert by_repo.autonomous_merge_rate.denominator == 1
    assert by_repo.repository == "org/web"
    assert _report(factory, source="tracker").autonomous_merge_rate.denominator == 1
    assert _report(factory, source="api").autonomous_merge_rate.numerator == 1
