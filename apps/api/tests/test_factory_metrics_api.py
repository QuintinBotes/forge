"""Factory metrics evidence from signed GitHub deliveries, and the report route (#196)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_api.routers.integration import (
    get_feedback_session_factory,
    get_github_webhook_secret,
)
from scf_api.routers.observability import get_factory_metrics_reader
from scf_contracts import UserRole
from scf_db.base import Base
from scf_db.models import (
    AgentRun,
    ApprovalGate,
    ApprovalRequest,
    ApprovalStatus,
    ChangeOutcome,
    Project,
    RunIntervention,
    RunStatus,
    Task,
    WorkflowRun,
    Workspace,
)
from scf_integrations import sign_github_payload
from scf_obs.analytics.factory import SqlFactoryMetricsReader

WS = uuid.UUID("00000000-0000-0000-0000-0000000196b1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000196b2")
SECRET = "whsec_factory_metrics_test"
REPO = "org/api"
BOT = "scf-app[bot]"
T0 = datetime(2026, 6, 10, tzinfo=UTC)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add_all(
            [Workspace(id=WS, name="A", slug="a196"), Workspace(id=WS2, name="B", slug="b196")]
        )
        session.commit()
    yield sf
    engine.dispose()


@pytest.fixture
def client(factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_github_webhook_secret] = lambda: SECRET
    app.dependency_overrides[get_feedback_session_factory] = lambda: factory
    app.dependency_overrides[get_factory_metrics_reader] = lambda: SqlFactoryMetricsReader(factory)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=uuid.uuid4(),
        workspace_id=WS,
        role=UserRole.VIEWER,
        email="reader@scf.test",
        auth_method="test",
        scopes=["*"],
    )
    with TestClient(app) as tc:
        yield tc


def _run(
    factory: sessionmaker[Session],
    *,
    number: int | None,
    workspace_id: uuid.UUID = WS,
    status: RunStatus = RunStatus.SUCCEEDED,
) -> uuid.UUID:
    with factory() as session:
        run = AgentRun(
            workspace_id=workspace_id,
            status=status,
            inputs={"context": {"repo_full_name": REPO}},
            created_at=T0,
            completed_at=T0,
        )
        if number is not None:
            run.output = {"artifacts": {"pull_request": {"status": "opened", "number": number}}}
        session.add(run)
        session.commit()
        return run.id


def _deliver(client: TestClient, event: str, payload: dict[str, Any]) -> None:
    body = json.dumps({"repository": {"full_name": REPO}, **payload}).encode()
    resp = client.post(
        "/integration/github/webhooks",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": sign_github_payload(SECRET, body),
        },
    )
    assert resp.status_code == 200, resp.text


def _closed(
    number: int, *, merged: bool, at: str = "2026-06-11T10:00:00Z", body: str | None = None
) -> dict[str, Any]:
    pull_request: dict[str, Any] = {
        "number": number,
        "merged": merged,
        "closed_at": at,
        "merged_at": at if merged else None,
        "user": {"login": BOT},
        "head": {"ref": "scf/run-000000000000"},
        "body": body,
    }
    return {"action": "closed", "pull_request": pull_request}


def _rows[T](factory: sessionmaker[Session], model: type[T]) -> list[T]:
    with factory() as session:
        return list(session.scalars(select(model)).all())


# --- criterion 4: the outcome, once ------------------------------------------ #


def test_a_closed_delivery_stores_the_outcome_and_its_time_once(factory, client) -> None:
    landed = _run(factory, number=7)
    closed = _run(factory, number=8)
    _deliver(client, "pull_request", _closed(7, merged=True))
    _deliver(client, "pull_request", _closed(8, merged=False, at="2026-06-12T09:00:00Z"))

    by_run = {row.agent_run_id: row for row in _rows(factory, ChangeOutcome)}
    assert by_run[landed].outcome == "landed"
    assert by_run[landed].outcome_at.replace(tzinfo=UTC) == datetime(2026, 6, 11, 10, tzinfo=UTC)
    assert (by_run[landed].repo_full_name, by_run[landed].pr_number) == (REPO, 7)
    assert by_run[closed].outcome == "closed"

    before = [(r.id, r.outcome, r.outcome_at, r.updated_at) for r in _rows(factory, ChangeOutcome)]
    _deliver(client, "pull_request", _closed(7, merged=True))
    _deliver(client, "pull_request", _closed(8, merged=False, at="2026-06-12T09:00:00Z"))
    after = [(r.id, r.outcome, r.outcome_at, r.updated_at) for r in _rows(factory, ChangeOutcome)]
    assert after == before


def test_a_reopened_pull_request_that_lands_ends_landed(factory, client) -> None:
    run_id = _run(factory, number=9)
    _deliver(client, "pull_request", _closed(9, merged=False, at="2026-06-11T10:00:00Z"))
    _deliver(client, "pull_request", _closed(9, merged=True, at="2026-06-13T10:00:00Z"))
    # A late redelivery of the first close does not undo the landing.
    _deliver(client, "pull_request", _closed(9, merged=False, at="2026-06-11T10:00:00Z"))
    (row,) = _rows(factory, ChangeOutcome)
    assert (row.agent_run_id, row.outcome) == (run_id, "landed")
    assert row.outcome_at.replace(tzinfo=UTC) == datetime(2026, 6, 13, 10, tzinfo=UTC)


def test_a_pull_request_of_no_run_records_nothing(factory, client) -> None:
    _run(factory, number=7)
    _deliver(client, "pull_request", _closed(404, merged=True))
    assert _rows(factory, ChangeOutcome) == []


# --- criterion 5: each intervention kind, once -------------------------------- #


def _review(number: int, *, state: str, review_id: int) -> dict[str, Any]:
    return {
        "action": "submitted",
        "review": {"id": review_id, "state": state, "submitted_at": "2026-06-11T08:00:00Z"},
        "pull_request": {"number": number, "user": {"login": BOT}, "head": {"ref": "x"}},
    }


def _push(number: int, *, sender: str, after: str) -> dict[str, Any]:
    return {
        "action": "synchronize",
        "after": after,
        "sender": {"login": sender},
        "pull_request": {
            "number": number,
            "user": {"login": BOT},
            "head": {"ref": "x"},
            "updated_at": "2026-06-11T09:00:00Z",
        },
    }


def test_a_changes_requested_review_is_one_intervention(factory, client) -> None:
    run_id = _run(factory, number=7)
    _deliver(client, "pull_request_review", _review(7, state="changes_requested", review_id=11))
    _deliver(client, "pull_request_review", _review(7, state="changes_requested", review_id=11))
    _deliver(client, "pull_request_review", _review(7, state="approved", review_id=12))
    _deliver(client, "pull_request_review", _review(7, state="commented", review_id=13))
    rows = _rows(factory, RunIntervention)
    assert [(r.agent_run_id, r.kind) for r in rows] == [(run_id, "review_changes_requested")]


def test_a_push_by_another_identity_is_one_intervention_and_scf_pushes_are_not(
    factory, client
) -> None:
    run_id = _run(factory, number=7)
    _deliver(client, "pull_request", _push(7, sender=BOT, after="a" * 40))
    _deliver(client, "pull_request", _push(7, sender="a-person", after="b" * 40))
    _deliver(client, "pull_request", _push(7, sender="a-person", after="b" * 40))
    rows = _rows(factory, RunIntervention)
    assert [(r.agent_run_id, r.kind) for r in rows] == [(run_id, "foreign_commit")]


def test_every_intervention_kind_is_reported_once_and_an_approval_is_none(factory, client) -> None:
    review = _run(factory, number=1)
    push = _run(factory, number=2)
    gate = _run(factory, number=3)
    approved = _run(factory, number=4)
    escalated = _run(factory, number=None, status=RunStatus.ESCALATED)
    with factory() as session:
        for run_id, status in (
            (gate, ApprovalStatus.REJECTED),
            (approved, ApprovalStatus.APPROVED),
        ):
            session.add(
                ApprovalRequest(
                    workspace_id=WS, agent_run_id=run_id, gate=ApprovalGate.PR, status=status
                )
            )
        session.commit()
    _deliver(client, "pull_request_review", _review(1, state="changes_requested", review_id=5))
    _deliver(client, "pull_request", _push(2, sender="a-person", after="c" * 40))
    for number in (1, 2, 3, 4):
        _deliver(client, "pull_request", _closed(number, merged=True))

    reader = SqlFactoryMetricsReader(factory)
    attempts = {
        a.run_id: a.interventions
        for a in reader.attempts(
            workspace_id=WS,
            since=datetime(2026, 6, 1, tzinfo=UTC),
            until=datetime(2026, 7, 1, tzinfo=UTC),
        )
    }
    assert attempts == {
        review: ("review_changes_requested",),
        push: ("foreign_commit",),
        gate: ("gate_decision",),
        approved: (),
        escalated: ("escalated",),
    }


# --- criterion 6: reverts ----------------------------------------------------- #


def test_a_landed_revert_marks_the_original_change_reverted(factory, client) -> None:
    original = _run(factory, number=7)
    _deliver(client, "pull_request", _closed(7, merged=True, at="2026-06-11T10:00:00Z"))
    # GitHub's revert button: a pull request (of no run here) whose body names the original.
    revert = _closed(
        30, merged=True, at="2026-06-20T10:00:00Z", body=f"Reverts {REPO}#7\n\nIt broke."
    )
    _deliver(client, "pull_request", revert)
    (row,) = _rows(factory, ChangeOutcome)
    assert row.agent_run_id == original
    assert row.reverted_at is not None
    assert row.reverted_at.replace(tzinfo=UTC) == datetime(2026, 6, 20, 10, tzinfo=UTC)
    assert row.reverted_by_pr_number == 30

    # The revert counts against the window in which the original landed.
    june = client.get(
        "/observability/analytics/factory",
        params={"since": "2026-06-01T00:00:00Z", "until": "2026-06-15T00:00:00Z"},
    ).json()
    assert june["revert_rate"]["numerator"] == 1
    assert june["revert_rate"]["denominator"] == 1


def test_an_unmerged_revert_or_another_repository_marks_nothing(factory, client) -> None:
    _run(factory, number=7)
    _deliver(client, "pull_request", _closed(7, merged=True))
    _deliver(client, "pull_request", _closed(31, merged=False, body=f"Reverts {REPO}#7"))
    _deliver(client, "pull_request", _closed(32, merged=True, body="Reverts other/repo#7"))
    (row,) = _rows(factory, ChangeOutcome)
    assert row.reverted_at is None


# --- criterion 7: the route --------------------------------------------------- #


def test_the_route_reports_five_metrics_for_the_callers_workspace_only(factory, client) -> None:
    _run(factory, number=7)
    _run(factory, number=8)
    _deliver(client, "pull_request", _closed(7, merged=True))
    _deliver(client, "pull_request", _closed(8, merged=False))
    with factory() as session:
        session.add(
            AgentRun(
                workspace_id=WS2,
                status=RunStatus.SUCCEEDED,
                inputs={"context": {"repo_full_name": REPO}},
                created_at=T0,
                completed_at=T0,
            )
        )
        session.commit()

    resp = client.get(
        "/observability/analytics/factory",
        params={"since": "2026-06-01T00:00:00Z", "until": "2026-07-01T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["since"].startswith("2026-06-01") and body["until"].startswith("2026-07-01")
    metrics = [value for value in body.values() if isinstance(value, dict)]
    assert len(metrics) == 5  # the five metrics, each with what it was computed from
    assert all({"value", "numerator", "denominator", "sample_size"} <= m.keys() for m in metrics)
    amr = body["autonomous_merge_rate"]
    assert (amr["value"], amr["numerator"], amr["denominator"]) == (0.5, 1, 2)


def test_the_route_applies_repository_and_source_filters(factory, client) -> None:
    _run(factory, number=7)
    _deliver(client, "pull_request", _closed(7, merged=True))
    window = {"since": "2026-06-01T00:00:00Z", "until": "2026-07-01T00:00:00Z"}

    other = client.get(
        "/observability/analytics/factory", params={**window, "repository": "org/web"}
    ).json()
    assert other["autonomous_merge_rate"]["available"] is False
    assert other["repository"] == "org/web"
    tracker = client.get(
        "/observability/analytics/factory", params={**window, "source": "tracker"}
    ).json()
    assert tracker["autonomous_merge_rate"]["denominator"] == 0
    api = client.get("/observability/analytics/factory", params={**window, "source": "api"})
    assert api.json()["autonomous_merge_rate"]["denominator"] == 1

    bad = client.get("/observability/analytics/factory", params={**window, "source": "cron"})
    assert bad.status_code == 422
    backwards = client.get(
        "/observability/analytics/factory",
        params={"since": window["until"], "until": window["since"]},
    )
    assert backwards.status_code == 422
    # A bound without an offset is UTC, so it compares with one that has one.
    naive = client.get(
        "/observability/analytics/factory",
        params={"since": "2026-06-01T00:00:00", "until": window["until"]},
    )
    assert naive.status_code == 200
    assert naive.json()["autonomous_merge_rate"]["denominator"] == 1
    future = client.get(
        "/observability/analytics/factory", params={"since": "2999-01-01T00:00:00Z"}
    )
    assert future.status_code == 422


# --- review fixes ------------------------------------------------------------- #


def _settled(client: TestClient) -> tuple[int, int]:
    body = client.get("/observability/analytics/factory", params=WINDOW).json()
    return body["autonomous_merge_rate"]["numerator"], body["autonomous_merge_rate"]["denominator"]


WINDOW = {"since": "2026-06-01T00:00:00Z", "until": "2026-07-01T00:00:00Z"}


def _governed(factory: sessionmaker[Session], *, pr: int | None) -> tuple[uuid.UUID, str]:
    """A governed builder run, a retry before it, and the workflow run that opens the PR."""
    with factory() as session:
        project = Project(workspace_id=WS, name="P", key=f"G{uuid.uuid4().hex[:6]}")
        session.add(project)
        session.flush()
        task = Task(workspace_id=WS, project_id=project.id, key="G-1", title="T")
        session.add(task)
        session.flush()
        context = {"pull_request": {"number": pr, "url": "u"}} if pr else {}
        workflow = WorkflowRun(workspace_id=WS, task_id=task.id, context=context)
        session.add(workflow)
        session.flush()
        for minutes in (0, 5):  # the retry, then the run the workflow delivers
            run = AgentRun(
                workspace_id=WS,
                workflow_run_id=workflow.id,
                status=RunStatus.SUCCEEDED,
                inputs={"context": {"repo_full_name": REPO}},
                created_at=T0.replace(minute=minutes),
                completed_at=T0,
            )
            session.add(run)
            session.flush()
        session.commit()
        return run.id, f"scf/run-{run.id.hex[:12]}"


def _end_workflow(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        for workflow in session.scalars(select(WorkflowRun)):
            workflow.status, workflow.completed_at = RunStatus.SUCCEEDED, T0
        session.commit()


def test_a_governed_attempt_settles_only_when_its_workflow_and_pull_request_close(
    factory, client
) -> None:
    run_id, branch = _governed(factory, pr=7)
    assert _settled(client) == (0, 0)  # the workflow run is still running
    _end_workflow(factory)
    assert _settled(client) == (0, 1)  # the retry settled; the delivered PR is open
    landed = _closed(7, merged=True)
    landed["pull_request"]["head"] = {"ref": branch}
    _deliver(client, "pull_request", landed)
    assert [r.agent_run_id for r in _rows(factory, ChangeOutcome)] == [run_id]
    assert _settled(client) == (1, 2)


def test_a_revert_branch_never_resolves_to_the_run_it_reverts(factory, client) -> None:
    run_id, branch = _governed(factory, pr=None)
    for number, ref in ((30, f"revert-7-{branch}"), (7, branch)):
        review = _review(number, state="changes_requested", review_id=number)
        review["pull_request"]["head"] = {"ref": ref}
        _deliver(client, "pull_request_review", review)
    assert [(r.agent_run_id, r.source_key) for r in _rows(factory, RunIntervention)] == [
        (run_id, "review:7")
    ]


def test_a_reopened_pull_request_is_open_until_it_closes_again(factory, client) -> None:
    _run(factory, number=9)
    pull_request = {"number": 9, "head": {"ref": "x"}, "updated_at": "2026-06-12T10:00:00Z"}
    reopen = {"action": "reopened", "pull_request": pull_request}
    _deliver(client, "pull_request", _closed(9, merged=False, at="2026-06-11T10:00:00Z"))
    _deliver(client, "pull_request", reopen)
    assert _rows(factory, ChangeOutcome) == []
    assert _settled(client) == (0, 0)
    _deliver(client, "pull_request", _closed(9, merged=False, at="2026-06-13T10:00:00Z"))
    _deliver(client, "pull_request", reopen)  # a late redelivery of the reopen
    (row,) = _rows(factory, ChangeOutcome)
    assert row.outcome_at.replace(tzinfo=UTC) == datetime(2026, 6, 13, 10, tzinfo=UTC)


def test_a_delivery_looks_its_run_up_once(factory, client, monkeypatch) -> None:
    from scf_api.routers import integration
    from scf_api.services import change_outcomes, context_service

    calls: list[object] = []
    real = context_service.resolve_run_for_pull_request
    spy = lambda *a, **k: calls.append(k) or real(*a, **k)  # noqa: E731
    for module in (context_service, change_outcomes, integration):
        monkeypatch.setattr(module, "resolve_run_for_pull_request", spy, raising=False)
    _run(factory, number=7)
    _deliver(client, "pull_request", _closed(7, merged=True))
    assert len(calls) == 1
