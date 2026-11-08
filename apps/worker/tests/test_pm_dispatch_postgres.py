"""Postgres race coverage for issue-driven dispatch (spec M M3).

Two concurrent ``maybe_dispatch`` evaluations must not both start a run: the
dispatch session takes a ``SELECT ... FOR UPDATE`` row lock on the
``PMConnection`` and re-runs the guard under it, so a second evaluation for
the same issue, the same delivery, or past the ``max_active_runs`` cap is
skipped. Skips cleanly (parked) without ``FORGE_TEST_DATABASE_URL`` — see the
root ``conftest.py`` ``pg_engine`` fixture.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.crypto import HmacAeadCipher
from forge_api.auth.vault import SecretVault
from forge_api.observability.audit import AuditLog
from forge_api.services import pm_service as pm_service_module
from forge_api.services.pm_service import PMConnectionService
from forge_contracts import AgentObjective
from forge_contracts.enums import Direction
from forge_contracts.pm import ExternalTask, StatusCategory
from forge_contracts.pm import PMProvider as ContractsProvider
from forge_db.base import Base
from forge_db.models import AgentRun, Project, RepositoryConnection, Workspace
from forge_db.models.enums import PMConnectionStatus, PMDeliveryStatus
from forge_db.models.enums import PMProvider as DbPMProvider
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_db.models.pm import PMConnection, PMWebhookDelivery
from forge_worker.tasks.pm_dispatch import DispatchOutcome, maybe_dispatch

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]

WS_ID = uuid.uuid4()


class _SlowFakeAdapter:
    """GitHub-Issues-shaped fake; the fetch delay widens the race window."""

    provider = ContractsProvider.github_issues

    def map_status(self, value: str, direction: Direction) -> str:
        return StatusCategory.started.value

    def map_priority(self, value: str, direction: Direction) -> str:
        return value

    def map_fields(self, fields: dict, direction: Direction) -> dict:
        return dict(fields)

    async def fetch_external(self, external_id: str) -> ExternalTask:
        # The guard/insert race window includes this fetch; hold it open so
        # both threads pass the first (unlocked) guard before either inserts.
        time.sleep(0.3)
        return ExternalTask(
            provider=ContractsProvider.github_issues,
            external_id=external_id,
            external_key=f"acme/widgets#{external_id}",
            url=f"https://github.com/acme/widgets/issues/{external_id}",
            title="Make widgets accessible",
            description_md="Fix the widget.",
            status_name="open",
            status_category=StatusCategory.unstarted,
            labels=["forge:run"],
            external_updated_at=datetime(2026, 11, 7, 10, 0, 0, tzinfo=UTC),
        )


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def connections(factory: sessionmaker[Session]) -> PMConnectionService:
    return PMConnectionService(
        session_factory=factory,
        vault=SecretVault(cipher=HmacAeadCipher(b"0" * 32)),
        audit=AuditLog(),
        transport_factory=lambda connection: object(),  # type: ignore[arg-type,return-value]
    )


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> _SlowFakeAdapter:
    adapter = _SlowFakeAdapter()

    def _fake_build_adapter(
        provider: object, transport: object, ctx: object, *, auth_header: str | None = None
    ) -> _SlowFakeAdapter:
        return adapter

    monkeypatch.setattr("forge_integrations.pm.registry.build_adapter", _fake_build_adapter)
    monkeypatch.setattr(pm_service_module, "build_adapter", _fake_build_adapter)
    return adapter


def _payload() -> dict:
    return {
        "action": "labeled",
        "event": "issues",
        "label": "forge:run",
        "sender_login": "octocat",
        "repository_full_name": "acme/widgets",
        "is_pull_request": False,
        "forge_run_marker": None,
    }


def _seed(
    factory: sessionmaker[Session], *, max_active_runs: int = 2
) -> tuple[uuid.UUID, uuid.UUID]:
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        session.flush()
        project = Project(workspace_id=WS_ID, name="Core", key=f"CORE{uuid.uuid4().hex[:4]}")
        session.add(project)
        session.flush()
        session.add(
            RepositoryConnection(
                workspace_id=WS_ID, repo_id="acme/widgets", name="widgets", is_active=True
            )
        )
        conn = PMConnection(
            workspace_id=WS_ID,
            provider=DbPMProvider.GITHUB_ISSUES,
            name="GitHub Issues",
            project_id=project.id,
            external_project_key="acme/widgets",
            external_project_id="10",
            config={
                "dispatch": {
                    "enabled": True,
                    "label": "forge:run",
                    "repository": "acme/widgets",
                    "allowed_senders": ["octocat"],
                    "max_active_runs": max_active_runs,
                    "status_on_pr_opened": None,
                }
            },
            status=PMConnectionStatus.CONNECTED,
        )
        session.add(conn)
        session.commit()
        return project.id, conn.id


def _seed_delivery(
    factory: sessionmaker[Session],
    connection_id: uuid.UUID,
    *,
    delivery_id: str,
    external_id: str = "42",
) -> PMWebhookDelivery:
    with factory() as session:
        row = PMWebhookDelivery(
            provider=DbPMProvider.GITHUB_ISSUES,
            connection_id=connection_id,
            delivery_id=delivery_id,
            event_type="issue.updated",
            external_id=external_id,
            payload_hash=f"hash-{delivery_id}",
            signature_valid=True,
            received_at=datetime.now(UTC),
            status=PMDeliveryStatus.RECEIVED,
        )
        session.add(row)
        session.commit()
        session.expunge(row)
        return row


def _run_concurrently(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection: PMConnection,
    deliveries: list[PMWebhookDelivery],
) -> tuple[list[DispatchOutcome], list[AgentObjective]]:
    barrier = threading.Barrier(len(deliveries))
    lock = threading.Lock()
    outcomes: list[DispatchOutcome] = []
    enqueued: list[AgentObjective] = []
    errors: list[BaseException] = []

    def _append_enqueued(objective: AgentObjective) -> None:
        with lock:
            enqueued.append(objective)

    def _worker(delivery: PMWebhookDelivery) -> None:
        try:
            barrier.wait(timeout=30)
            outcome = maybe_dispatch(
                factory,
                connection=connection,
                delivery=delivery,
                payload=_payload(),
                connections=connections,
                enqueue_run=_append_enqueued,
            )
            with lock:
                outcomes.append(outcome)
        except BaseException as exc:  # surfaced in the main thread below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(delivery,)) for delivery in deliveries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert len(outcomes) == len(deliveries)
    return outcomes, enqueued


def _active_runs(factory: sessionmaker[Session]) -> list[AgentRun]:
    with factory() as session:
        return list(
            session.scalars(
                select(AgentRun).where(
                    AgentRun.status.in_((DbRunStatus.PENDING, DbRunStatus.RUNNING))
                )
            )
        )


def test_concurrent_dispatch_same_delivery_and_issue_and_cap(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    fake_adapter: _SlowFakeAdapter,
) -> None:
    _, connection_id = _seed(factory)
    connection = connections.get_connection_any_workspace(connection_id)
    assert connection is not None

    # Same delivery, two threads: at most one run (per-delivery idempotency).
    delivery = _seed_delivery(factory, connection_id, delivery_id="gh-race")
    outcomes, enqueued = _run_concurrently(factory, connections, connection, [delivery, delivery])
    assert sorted(outcome.action for outcome in outcomes) == ["dispatched", "skipped"]
    skipped = [outcome for outcome in outcomes if outcome.action == "skipped"]
    assert skipped[0].reason in ("already_dispatched", "issue_run_active")
    assert len(enqueued) == 1
    assert len(_active_runs(factory)) == 1

    # Same issue, two distinct deliveries: still one active run per issue.
    outcomes, enqueued = _run_concurrently(
        factory,
        connections,
        connection,
        [
            _seed_delivery(factory, connection_id, delivery_id="gh-race-2"),
            _seed_delivery(factory, connection_id, delivery_id="gh-race-3"),
        ],
    )
    assert all(outcome.action == "skipped" for outcome in outcomes)
    assert {outcome.reason for outcome in outcomes} == {"issue_run_active"}
    assert enqueued == []

    # Finish the first run; the cap (2) then admits two fresh issues, not three.
    with factory() as session:
        for run in session.scalars(select(AgentRun)):
            run.status = DbRunStatus.SUCCEEDED
        session.commit()
    outcomes, enqueued = _run_concurrently(
        factory,
        connections,
        connection,
        [
            _seed_delivery(factory, connection_id, delivery_id="gh-cap-1", external_id="43"),
            _seed_delivery(factory, connection_id, delivery_id="gh-cap-2", external_id="44"),
            _seed_delivery(factory, connection_id, delivery_id="gh-cap-3", external_id="45"),
        ],
    )
    assert sorted(outcome.action for outcome in outcomes) == [
        "dispatched",
        "dispatched",
        "skipped",
    ]
    skipped = [outcome for outcome in outcomes if outcome.action == "skipped"]
    assert skipped[0].reason == "max_active_runs_reached"
    assert len(enqueued) == 2
    assert len(_active_runs(factory)) == 2
    assert all(
        (run.inputs or {}).get("context", {}).get("tracker") is not None
        for run in _active_runs(factory)
    )


def test_concurrent_writeback_claims_admit_exactly_one(factory: sessionmaker[Session]) -> None:
    """The claim locks the run row, so two finishing executions cannot both post."""
    from forge_worker.tasks.pm_dispatch import _claim_writeback

    _seed(factory)
    with factory() as session:
        run = AgentRun(workspace_id=WS_ID, status=DbRunStatus.SUCCEEDED, output={})
        session.add(run)
        session.commit()
        run_id = run.id

    for _ in range(20):
        with factory() as session:
            row = session.get(AgentRun, run_id)
            assert row is not None
            row.output = {}
            session.commit()
        barrier = threading.Barrier(2)
        won: list[bool] = []

        def claim(barrier: threading.Barrier = barrier, won: list[bool] = won) -> None:
            barrier.wait()
            won.append(_claim_writeback(factory, run_id))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(won) == [False, True]
