"""Spec M M3/M2 worker path: issue-driven dispatch + run write-back.

Covers acceptance criteria 2, 4 and 6 at the worker seam
(``forge.pm.process_webhook`` + ``scf_worker.tasks.pm_dispatch``):

* an ``issues.labeled`` delivery with ``forge:run`` on a connected repository
  from an allowed sender enqueues exactly one run whose objective is built
  from the *re-fetched* issue (never the webhook payload); a replayed
  delivery and a re-queued dispatch each enqueue none;
* every fail-closed refusal (disabled, disallowed sender, unconnected or
  unconfigured repository, pull request, another repo, different label, one
  active run per issue, per-connection cap) enqueues none and records its
  reason on the delivery;
* a finished tracker-born run posts exactly one comment (state + PR link +
  hidden marker), applies ``status_on_pr_opened`` only when a PR opened, and
  a write-back failure never changes the run's recorded outcome; the echo of
  that comment is skipped;
* tracker text cannot set server-owned context; the body is truncated.

Hermetic: in-memory SQLite (StaticPool), fake adapter via the registry seam,
no Celery broker, no sockets.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.auth.crypto import HmacAeadCipher
from scf_api.auth.vault import SecretVault
from scf_api.observability.audit import AuditLog
from scf_api.services import pm_service as pm_service_module
from scf_api.services.pm_service import PMConnectionService
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_contracts.enums import Direction
from scf_contracts.pm import ExternalTask, StatusCategory
from scf_contracts.pm import PMProvider as ContractsProvider
from scf_db.base import Base
from scf_db.models import AgentRun, Project, RepositoryConnection, Task, Workspace
from scf_db.models.enums import PMConnectionStatus, PMDeliveryStatus, PMSyncDirection
from scf_db.models.enums import PMProvider as DbPMProvider
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_db.models.pm import PMConnection, PMWebhookDelivery
from scf_worker.agent_runner import _maybe_tracker_writeback
from scf_worker.celery_app import celery_app
from scf_worker.tasks.pm_dispatch import PM_DISPATCH_TASK, maybe_dispatch, post_run_writeback
from scf_worker.tasks.pm_sync import process_webhook

WS_ID = uuid.uuid4()

BODY = (
    "Fix the widget.\n\n"
    "## Acceptance criteria\n"
    "- [ ] Widget renders\n"
    "- Tests pass\n\n"
    "## Notes\n"
    "- not a criterion\n"
)


class FakeGitHubAdapter:
    """A controllable GitHub-Issues-shaped adapter behind the registry seam."""

    provider = ContractsProvider.github_issues

    def __init__(self) -> None:
        self.external: ExternalTask | None = None
        self.fetch_calls: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, StatusCategory]] = []
        self.raise_on_comment: Exception | None = None

    def map_status(self, value: str, direction: Direction) -> str:
        return StatusCategory.started.value

    def map_priority(self, value: str, direction: Direction) -> str:
        return value

    def map_fields(self, fields: dict, direction: Direction) -> dict:
        return dict(fields)

    async def fetch_external(self, external_id: str) -> ExternalTask:
        self.fetch_calls.append(external_id)
        assert self.external is not None, "fake adapter has no external staged"
        return self.external.model_copy(
            update={
                "external_id": external_id,
                "external_key": f"acme/widgets#{external_id}",
            }
        )

    async def comment_external(self, external_id: str, body: str) -> str:
        if self.raise_on_comment is not None:
            raise self.raise_on_comment
        self.comments.append((external_id, body))
        return "1001"

    async def transition_external(self, external_id: str, category: StatusCategory) -> ExternalTask:
        self.transitions.append((external_id, category))
        assert self.external is not None
        return self.external


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def project_id(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.flush()
        project = Project(workspace_id=WS_ID, name="Core", key="CORE")
        session.add(project)
        session.commit()
        return project.id


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> FakeGitHubAdapter:
    adapter = FakeGitHubAdapter()
    adapter.external = ExternalTask(
        provider=ContractsProvider.github_issues,
        external_id="42",
        external_key="acme/widgets#42",
        url="https://github.com/acme/widgets/issues/42",
        title="Make widgets accessible",
        description_md=BODY,
        status_name="open",
        status_category=StatusCategory.unstarted,
        labels=["forge:run", "bug"],
        external_updated_at=datetime(2026, 11, 7, 10, 0, 0, tzinfo=UTC),
    )

    def _fake_build_adapter(
        provider: object, transport: object, ctx: object, *, auth_header: str | None = None
    ) -> FakeGitHubAdapter:
        return adapter

    monkeypatch.setattr("scf_integrations.pm.registry.build_adapter", _fake_build_adapter)
    monkeypatch.setattr(pm_service_module, "build_adapter", _fake_build_adapter)
    return adapter


@pytest.fixture
def connections(factory: sessionmaker[Session]) -> PMConnectionService:
    return PMConnectionService(
        session_factory=factory,
        vault=SecretVault(cipher=HmacAeadCipher(b"0" * 32)),
        audit=AuditLog(),
        transport_factory=lambda connection: object(),  # type: ignore[arg-type,return-value]
    )


def _dispatch_config(**overrides: Any) -> dict:
    config: dict[str, Any] = {
        "enabled": True,
        "label": "forge:run",
        "repository": "acme/widgets",
        "allowed_senders": ["octocat"],
        "max_active_runs": 2,
        "status_on_pr_opened": "started",
    }
    config.update(overrides)
    return config


def _seed_connection(
    factory: sessionmaker[Session],
    project_id: uuid.UUID,
    *,
    dispatch: dict | None = None,
    connect_repo: bool = True,
    external_project_key: str = "acme/widgets",
) -> uuid.UUID:
    with factory() as session:
        if connect_repo:
            session.add(
                RepositoryConnection(
                    workspace_id=WS_ID, repo_id="acme/widgets", name="widgets", is_active=True
                )
            )
        conn = PMConnection(
            workspace_id=WS_ID,
            provider=DbPMProvider.GITHUB_ISSUES,
            name="GitHub Issues",
            project_id=project_id,
            external_project_key=external_project_key,
            external_project_id="10",
            config={"dispatch": _dispatch_config() if dispatch is None else dispatch},
            status=PMConnectionStatus.CONNECTED,
        )
        session.add(conn)
        session.commit()
        return conn.id


def _seed_delivery(
    factory: sessionmaker[Session],
    connection_id: uuid.UUID,
    *,
    delivery_id: str = "gh-1",
    external_id: str | None = "42",
    payload_hash: str = "deadbeef",
) -> uuid.UUID:
    with factory() as session:
        row = PMWebhookDelivery(
            provider=DbPMProvider.GITHUB_ISSUES,
            connection_id=connection_id,
            delivery_id=delivery_id,
            event_type="issue.updated",
            external_id=external_id,
            payload_hash=payload_hash,
            signature_valid=True,
            received_at=datetime.now(UTC),
            status=PMDeliveryStatus.RECEIVED,
        )
        session.add(row)
        session.commit()
        return row.id


def _payload(**overrides: Any) -> dict:
    payload: dict[str, Any] = {
        "action": "labeled",
        "event": "issues",
        "label": "forge:run",
        "sender_login": "octocat",
        "repository_full_name": "acme/widgets",
        "is_pull_request": False,
        "forge_run_marker": None,
    }
    payload.update(overrides)
    return payload


def _runs(factory: sessionmaker[Session]) -> list[AgentRun]:
    with factory() as session:
        rows = session.execute(select(AgentRun)).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _delivery(factory: sessionmaker[Session], row_id: uuid.UUID) -> PMWebhookDelivery:
    with factory() as session:
        row = session.get(PMWebhookDelivery, row_id)
        assert row is not None
        session.expunge(row)
        return row


# --------------------------------------------------------------------------- #
# Dispatch (acceptance 2 + 6)                                                 #
# --------------------------------------------------------------------------- #


def test_labeled_webhook_dispatches_exactly_one_run_from_refetched_issue(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id)
    enqueued: list[AgentObjective] = []

    result = process_webhook(
        factory,
        row_id,
        connections=connections,
        # Hostile extras in the hint payload must never become the objective.
        payload=_payload(title="PAYLOAD TITLE", description="payload body"),
        enqueue_run=enqueued.append,
    )

    assert result["dispatch_action"] == "dispatched"
    assert len(enqueued) == 1
    objective = enqueued[0]
    # Built from the re-fetched issue, not the payload (acceptance 6).
    assert fake_adapter.fetch_calls == ["42", "42"]  # board sync + dispatch re-fetch
    assert objective.objective == "Make widgets accessible"
    assert objective.description == BODY
    assert [c.text for c in objective.acceptance_criteria] == ["Widget renders", "Tests pass"]
    # Server-owned + provenance context, all set by SCF.
    runs = _runs(factory)
    assert len(runs) == 1
    assert runs[0].status == DbRunStatus.PENDING
    context = objective.context
    assert context["agent_run_id"] == str(runs[0].id) == result["run_id"]
    assert context["workspace_id"] == str(WS_ID)
    assert context["repo_full_name"] == "acme/widgets"
    assert context["source"] == "github_issues:acme/widgets#42"
    assert context["actor"] == f"pm:{connection_id}"
    assert context["tracker"]["sender_login"] == "octocat"
    assert context["tracker"]["delivery_id"] == "gh-1"
    assert context["tracker"]["connection_id"] == str(connection_id)
    assert objective.task_id is not None  # linked board task from the sync half

    # A replayed delivery (same row reprocessed) enqueues none.
    replay = process_webhook(
        factory,
        row_id,
        connections=connections,
        payload=_payload(),
        enqueue_run=enqueued.append,
    )
    assert replay["action"] == "already_processed"
    assert len(enqueued) == 1
    assert len(_runs(factory)) == 1


def test_requeued_dispatch_cannot_enqueue_twice(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id)
    enqueued: list[AgentObjective] = []
    connection = connections.get_connection_any_workspace(connection_id)
    assert connection is not None
    delivery = _delivery(factory, row_id)

    first = maybe_dispatch(
        factory,
        connection=connection,
        delivery=delivery,
        payload=_payload(),
        connections=connections,
        enqueue_run=enqueued.append,
    )
    second = maybe_dispatch(
        factory,
        connection=connection,
        delivery=delivery,
        payload=_payload(),
        connections=connections,
        enqueue_run=enqueued.append,
    )
    assert first.action == "dispatched"
    assert second.action == "skipped" and second.reason == "already_dispatched"
    assert len(enqueued) == 1


@pytest.mark.parametrize(
    ("dispatch", "payload_overrides", "external_id", "connect_repo", "reason"),
    [
        ({"enabled": False}, {}, "42", True, "dispatch_disabled"),
        ({}, {"sender_login": "mallory"}, "42", True, "sender_not_allowed"),
        ({}, {"sender_login": None}, "42", True, "sender_not_allowed"),
        ({}, {}, "42", False, "repository_not_connected"),
        ({"repository": None}, {}, "42", True, "repository_not_configured"),
        ({}, {"is_pull_request": True}, None, True, "pull_request"),
        ({}, {"repository_full_name": "other/repo"}, None, True, "repository_mismatch"),
        ({}, {"label": "enhancement"}, "42", True, "label_mismatch"),
        ({"max_active_runs": 99}, {}, "42", True, "invalid_dispatch_config"),
    ],
)
def test_dispatch_refusals_enqueue_none_and_record_why(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    dispatch: dict,
    payload_overrides: dict,
    external_id: str | None,
    connect_repo: bool,
    reason: str,
) -> None:
    connection_id = _seed_connection(
        factory,
        project_id,
        dispatch=_dispatch_config(**dispatch),
        connect_repo=connect_repo,
    )
    row_id = _seed_delivery(factory, connection_id, external_id=external_id)
    enqueued: list[AgentObjective] = []

    result = process_webhook(
        factory,
        row_id,
        connections=connections,
        payload=_payload(**payload_overrides),
        enqueue_run=enqueued.append,
    )

    assert result["dispatch_action"] == "skipped"
    assert result["dispatch_reason"] == reason
    assert enqueued == []
    assert _runs(factory) == []
    assert _delivery(factory, row_id).error == f"dispatch_skipped: {reason}"


def test_allowed_sender_match_is_case_insensitive(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(
        factory, project_id, dispatch=_dispatch_config(allowed_senders=["OctoCat"])
    )
    row_id = _seed_delivery(factory, connection_id)
    enqueued: list[AgentObjective] = []
    result = process_webhook(
        factory,
        row_id,
        connections=connections,
        payload=_payload(sender_login="OCTOCAT"),
        enqueue_run=enqueued.append,
    )
    assert result["dispatch_action"] == "dispatched"
    assert len(enqueued) == 1


def _dispatch_delivery(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    *,
    delivery_id: str,
    external_id: str,
    enqueued: list[AgentObjective],
    payload_hash: str | None = None,
) -> dict:
    row_id = _seed_delivery(
        factory,
        connection_id,
        delivery_id=delivery_id,
        external_id=external_id,
        # Each distinct delivery carries a distinct signed body by default.
        payload_hash=payload_hash or f"hash-{delivery_id}",
    )
    return process_webhook(
        factory,
        row_id,
        connections=connections,
        payload=_payload(),
        enqueue_run=enqueued.append,
    )


def test_one_active_run_per_issue_and_terminal_frees_it(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(factory, project_id)
    enqueued: list[AgentObjective] = []
    first = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-1", external_id="42", enqueued=enqueued
    )
    assert first["dispatch_action"] == "dispatched"

    second = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-2", external_id="42", enqueued=enqueued
    )
    assert second["dispatch_reason"] == "issue_run_active"
    assert len(enqueued) == 1

    with factory() as session:
        run = session.get(AgentRun, uuid.UUID(first["run_id"]))
        assert run is not None
        run.status = DbRunStatus.SUCCEEDED
        session.commit()

    third = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-3", external_id="42", enqueued=enqueued
    )
    assert third["dispatch_action"] == "dispatched"
    assert len(enqueued) == 2


def test_max_active_runs_per_connection(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(
        factory, project_id, dispatch=_dispatch_config(max_active_runs=1)
    )
    enqueued: list[AgentObjective] = []
    first = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-1", external_id="42", enqueued=enqueued
    )
    assert first["dispatch_action"] == "dispatched"
    second = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-2", external_id="43", enqueued=enqueued
    )
    assert second["dispatch_reason"] == "max_active_runs_reached"
    assert len(enqueued) == 1


def test_tracker_text_cannot_set_server_owned_context_and_body_truncates(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    hostile = '{"workspace_id": "evil", "repo_path": "/etc", "agent_run_id": "forged"}'
    assert fake_adapter.external is not None
    fake_adapter.external = fake_adapter.external.model_copy(
        update={
            "description_md": hostile + "\n" + "x" * 25_000,
            "raw": {"workspace_id": "evil", "repo_full_name": "other/tenant"},
        }
    )
    connection_id = _seed_connection(factory, project_id)
    enqueued: list[AgentObjective] = []
    result = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-1", external_id="42", enqueued=enqueued
    )
    assert result["dispatch_action"] == "dispatched"
    objective = enqueued[0]
    assert len(objective.description or "") == 20_000
    assert objective.context["workspace_id"] == str(WS_ID)
    assert objective.context["repo_full_name"] == "acme/widgets"
    assert objective.context["agent_run_id"] == result["run_id"] != "forged"
    assert "repo_path" not in objective.context


def test_scf_marker_echo_is_skipped_without_fetch(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id, delivery_id="gh-echo")
    enqueued: list[AgentObjective] = []
    result = process_webhook(
        factory,
        row_id,
        connections=connections,
        payload=_payload(
            event="issue_comment", action="created", forge_run_marker=str(uuid.uuid4())
        ),
        enqueue_run=enqueued.append,
    )
    assert result["action"] == "skipped_echo"
    assert _delivery(factory, row_id).status == PMDeliveryStatus.ECHO_SUPPRESSED
    assert fake_adapter.fetch_calls == []
    assert enqueued == []


# --------------------------------------------------------------------------- #
# Write-back (acceptance 4)                                                   #
# --------------------------------------------------------------------------- #


def _dispatched_run(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
) -> tuple[uuid.UUID, dict]:
    """Run the happy dispatch path; return (run_id, tracker block)."""
    connection_id = _seed_connection(factory, project_id)
    enqueued: list[AgentObjective] = []
    result = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-1", external_id="42", enqueued=enqueued
    )
    assert result["dispatch_action"] == "dispatched"
    return uuid.UUID(result["run_id"]), enqueued[0].context["tracker"]


def test_finished_run_posts_one_comment_with_pr_link_and_marker(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    run_id, tracker = _dispatched_run(factory, connections, project_id)
    result = AgentRunResult(
        run_id=run_id,
        status=RunStatus.SUCCEEDED,
        artifacts={
            "pull_request": {
                "status": "opened",
                "number": 7,
                "url": "https://github.com/acme/widgets/pull/7",
            }
        },
    )

    status = post_run_writeback(
        factory,
        run_id=run_id,
        workspace_id=WS_ID,
        result=result,
        tracker=tracker,
        connections=connections,
    )

    assert status == "posted"
    assert len(fake_adapter.comments) == 1
    external_id, body = fake_adapter.comments[0]
    assert external_id == "42"
    assert "succeeded" in body
    assert "https://github.com/acme/widgets/pull/7" in body
    assert f"<!-- forge:run:{run_id} -->" in body
    # PR opened -> the mapped transition is applied exactly once.
    assert fake_adapter.transitions == [("42", StatusCategory.started)]

    # Exactly one comment: a redelivery is a no-op.
    again = post_run_writeback(
        factory,
        run_id=run_id,
        workspace_id=WS_ID,
        result=result,
        tracker=tracker,
        connections=connections,
    )
    assert again == "already_recorded"
    assert len(fake_adapter.comments) == 1


def test_writeback_without_pr_applies_no_transition(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    run_id, tracker = _dispatched_run(factory, connections, project_id)
    result = AgentRunResult(run_id=run_id, status=RunStatus.FAILED, error="boom")

    assert (
        post_run_writeback(
            factory,
            run_id=run_id,
            workspace_id=WS_ID,
            result=result,
            tracker=tracker,
            connections=connections,
        )
        == "posted"
    )
    assert len(fake_adapter.comments) == 1
    assert "failed" in fake_adapter.comments[0][1]
    assert fake_adapter.transitions == []


def test_writeback_failure_never_changes_recorded_outcome(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    run_id, tracker = _dispatched_run(factory, connections, project_id)
    with factory() as session:
        row = session.get(AgentRun, run_id)
        assert row is not None
        row.status = DbRunStatus.SUCCEEDED
        row.output = {"status": "succeeded", "summary": "done"}
        session.commit()
    fake_adapter.raise_on_comment = RuntimeError("tracker down")
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED)

    status = post_run_writeback(
        factory,
        run_id=run_id,
        workspace_id=WS_ID,
        result=result,
        tracker=tracker,
        connections=connections,
    )

    assert status == "failed"
    with factory() as session:
        row = session.get(AgentRun, run_id)
        assert row is not None
        assert row.status == DbRunStatus.SUCCEEDED  # outcome untouched
        assert row.output["summary"] == "done"
        assert row.output["tracker_writeback"]["status"] == "failed"


def test_agent_runner_writeback_hook_is_inert_without_tracker(
    factory: sessionmaker[Session],
) -> None:
    """Native-board runs (no tracker block) never touch a tracker (acceptance 5)."""
    objective = AgentObjective(objective="plain board run", context={"workspace_id": str(WS_ID)})
    result = AgentRunResult(run_id=uuid.uuid4(), status=RunStatus.SUCCEEDED)
    # connections=object() would explode if the hook tried to use it.
    _maybe_tracker_writeback(
        objective, uuid.uuid4(), WS_ID, result, session_factory=factory, connections=object()
    )


def test_non_github_connection_dispatch_is_not_applicable(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    with factory() as session:
        conn = PMConnection(
            workspace_id=WS_ID,
            provider=DbPMProvider.LINEAR,
            name="Linear",
            project_id=project_id,
            external_project_key="ENG",
            external_project_id="ENG",
            config={"dispatch": _dispatch_config()},
            status=PMConnectionStatus.CONNECTED,
        )
        session.add(conn)
        session.commit()
        session.expunge(conn)
    row_id = _seed_delivery(factory, conn.id)
    outcome = maybe_dispatch(
        factory,
        connection=conn,
        delivery=_delivery(factory, row_id),
        payload=_payload(),
        connections=connections,
        enqueue_run=[].append,  # type: ignore[arg-type]
    )
    assert outcome.action == "not_applicable"


def test_dispatch_task_registered() -> None:
    assert PM_DISPATCH_TASK == "forge.pm.dispatch_issue"
    assert PM_DISPATCH_TASK in celery_app.tasks


# --------------------------------------------------------------------------- #
# Correction pass (spec M review)                                             #
# --------------------------------------------------------------------------- #


def test_guard_blocks_replay_with_identical_signed_body(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    """A new delivery id carrying the same signed body cannot dispatch."""
    connection_id = _seed_connection(factory, project_id)
    enqueued: list[AgentObjective] = []
    first = _dispatch_delivery(
        factory,
        connections,
        connection_id,
        delivery_id="gh-1",
        external_id="42",
        enqueued=enqueued,
        payload_hash="signed-body-hash",
    )
    assert first["dispatch_action"] == "dispatched"
    with factory() as session:
        run = session.get(AgentRun, uuid.UUID(first["run_id"]))
        assert run is not None
        run.status = DbRunStatus.SUCCEEDED  # even after the first run finished
        session.commit()

    replay = _dispatch_delivery(
        factory,
        connections,
        connection_id,
        delivery_id="attacker-guid-2",
        external_id="42",
        enqueued=enqueued,
        payload_hash="signed-body-hash",
    )
    assert replay["dispatch_reason"] == "already_dispatched"
    assert len(enqueued) == 1


def test_url_form_project_key_dispatches(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(
        factory, project_id, external_project_key="https://github.com/acme/widgets"
    )
    enqueued: list[AgentObjective] = []
    result = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-1", external_id="42", enqueued=enqueued
    )
    assert result["dispatch_action"] == "dispatched"
    assert len(enqueued) == 1


def test_stale_pending_run_no_longer_blocks_issue_or_cap(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    """A PENDING run older than the hard limit + 10m counts as dead."""
    connection_id = _seed_connection(
        factory, project_id, dispatch=_dispatch_config(max_active_runs=1)
    )
    enqueued: list[AgentObjective] = []
    first = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-1", external_id="42", enqueued=enqueued
    )
    assert first["dispatch_action"] == "dispatched"
    with factory() as session:
        run = session.get(AgentRun, uuid.UUID(first["run_id"]))
        assert run is not None
        run.created_at = datetime.now(UTC) - timedelta(hours=3)
        run.started_at = datetime.now(UTC) - timedelta(hours=2)  # worker started, then died
        session.commit()

    second = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-2", external_id="42", enqueued=enqueued
    )
    assert second["dispatch_action"] == "dispatched"
    assert len(enqueued) == 2


def test_a_run_still_queued_keeps_blocking_its_issue(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    """No worker has started it yet: it counts as active for a day, not the hard limit."""
    connection_id = _seed_connection(
        factory, project_id, dispatch=_dispatch_config(max_active_runs=1)
    )
    enqueued: list[AgentObjective] = []
    first = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-1", external_id="42", enqueued=enqueued
    )
    with factory() as session:
        run = session.get(AgentRun, uuid.UUID(first["run_id"]))
        assert run is not None and run.started_at is None
        run.created_at = datetime.now(UTC) - timedelta(hours=2)  # long broker backlog
        session.commit()

    second = _dispatch_delivery(
        factory, connections, connection_id, delivery_id="gh-2", external_id="42", enqueued=enqueued
    )
    assert second["dispatch_action"] == "skipped"
    assert second["dispatch_reason"] == "issue_run_active"
    assert len(enqueued) == 1


def test_failed_broker_enqueue_writes_back_failed(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id, payload_hash="hash-enq")

    def _broker_down(_objective: AgentObjective) -> None:
        raise ConnectionError("broker down")

    result = process_webhook(
        factory,
        row_id,
        connections=connections,
        payload=_payload(),
        enqueue_run=_broker_down,
    )
    assert result["dispatch_reason"] == "enqueue_failed"
    runs = _runs(factory)
    assert len(runs) == 1 and runs[0].status == DbRunStatus.FAILED
    assert len(fake_adapter.comments) == 1
    assert "failed" in fake_adapter.comments[0][1]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda conn: setattr(conn, "status", PMConnectionStatus.DISABLED), "connection_disabled"),
        (
            lambda conn: setattr(conn, "sync_direction", PMSyncDirection.INBOUND_ONLY),
            "inbound_only",
        ),
    ],
)
def test_writeback_skipped_on_disabled_or_inbound_only_connection(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    mutate,
    reason: str,
) -> None:
    run_id, tracker = _dispatched_run(factory, connections, project_id)
    with factory() as session:
        conn = session.get(PMConnection, uuid.UUID(tracker["connection_id"]))
        assert conn is not None
        mutate(conn)
        session.commit()
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED)

    status = post_run_writeback(
        factory,
        run_id=run_id,
        workspace_id=WS_ID,
        result=result,
        tracker=tracker,
        connections=connections,
    )

    assert status == "skipped"
    assert fake_adapter.comments == [] and fake_adapter.transitions == []
    with factory() as session:
        row = session.get(AgentRun, run_id)
        assert row is not None
        assert row.output["tracker_writeback"] == {"status": "skipped", "reason": reason}


def test_writeback_claim_survives_outcome_rewrite(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
) -> None:
    """A redelivered run records its outcome again, then must not re-post."""
    from scf_worker.agent_runner import _record_run_outcome

    run_id, tracker = _dispatched_run(factory, connections, project_id)
    result = AgentRunResult(run_id=run_id, status=RunStatus.SUCCEEDED)
    assert (
        post_run_writeback(
            factory,
            run_id=run_id,
            workspace_id=WS_ID,
            result=result,
            tracker=tracker,
            connections=connections,
        )
        == "posted"
    )
    assert len(fake_adapter.comments) == 1

    _record_run_outcome(run_id, WS_ID, result, session_factory=factory)
    with factory() as session:
        row = session.get(AgentRun, run_id)
        assert row is not None
        assert row.output["tracker_writeback"]["status"] == "posted"

    assert (
        post_run_writeback(
            factory,
            run_id=run_id,
            workspace_id=WS_ID,
            result=result,
            tracker=tracker,
            connections=connections,
        )
        == "already_recorded"
    )
    assert len(fake_adapter.comments) == 1


# --------------------------------------------------------------------------- #
# Governed dispatch: the Temporal backend starts a workflow run (#171)        #
# --------------------------------------------------------------------------- #


class _FakeTemporalHandle:
    result_run_id = "fake-temporal-run"


class _FakeTemporalClient:
    """Records the workflows started; nothing runs (the workflow's own tests do).

    Like Temporal under ``REJECT_DUPLICATE``, it refuses a workflow id it started.
    """

    def __init__(self) -> None:
        self.started: list[Any] = []
        self._ids: set[str] = set()

    async def start_workflow(self, _workflow: Any, params: Any, **kw: Any) -> _FakeTemporalHandle:
        from temporalio.exceptions import WorkflowAlreadyStartedError

        if kw["id"] in self._ids:
            raise WorkflowAlreadyStartedError(kw["id"], "FeatureWorkflow")
        self._ids.add(kw["id"])
        self.started.append(params)
        return _FakeTemporalHandle()

    def get_workflow_handle(self, _wf_id: str) -> _FakeTemporalHandle:
        return _FakeTemporalHandle()


@pytest.fixture
def temporal(monkeypatch: pytest.MonkeyPatch) -> _FakeTemporalClient:
    from scf_api.services import workflow_engine as workflow_engine_module

    fake = _FakeTemporalClient()

    async def _get_client(_settings: object = None) -> _FakeTemporalClient:
        return fake

    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    monkeypatch.setattr(workflow_engine_module, "get_temporal_client", _get_client)
    return fake


def _workflow_runs(factory: sessionmaker[Session]) -> list[Any]:
    from scf_db.models.runs import WorkflowRun as DbWorkflowRun

    with factory() as session:
        rows = session.execute(select(DbWorkflowRun)).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def test_a_governed_dispatch_starts_one_workflow_run_and_enqueues_no_agent(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    temporal: _FakeTemporalClient,
) -> None:
    """#171 criterion 1 (and 3's record): the label starts a workflow run for the
    issue's task, named for its sender; no Celery agent run is enqueued."""
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id)
    enqueued: list[AgentObjective] = []

    result = process_webhook(
        factory, row_id, connections=connections, payload=_payload(), enqueue_run=enqueued.append
    )

    assert result["dispatch_action"] == "dispatched"
    assert enqueued == [] and _runs(factory) == [], "no agent runs before the gates"
    [run] = _workflow_runs(factory)
    assert result["run_id"] == str(run.id)
    assert run.task_id is not None  # the board task the sync half linked
    # Every step of the run reads the task's repository: it now names the one dispatched.
    with factory() as session:
        task = session.get(Task, run.task_id)
        assert task is not None
        assert [t["repo"] for t in task.repo_targets] == ["acme/widgets"]
    [params] = temporal.started
    assert params.workflow_run_id == run.id
    assert params.started_by == "github:octocat"
    trigger = run.context["trigger"]
    assert trigger["started_by"] == "github:octocat"
    assert trigger["tracker"]["delivery_id"] == "gh-1"
    assert trigger["tracker"]["connection_id"] == str(connection_id)
    # The objective comes from the re-fetched issue, kept for forge.run_agent.
    assert trigger["objective"]["objective"] == "Make widgets accessible"
    assert trigger["objective"]["description"] == BODY
    # The agent renders the issue's text as untrusted data under this label (ADR-0014).
    assert trigger["source_label"] == "github_issues:acme/widgets#42"

    # Another delivery for the same issue while the run is active is skipped.
    again = _seed_delivery(factory, connection_id, delivery_id="gh-2", payload_hash="cafe")
    second = process_webhook(
        factory, again, connections=connections, payload=_payload(), enqueue_run=enqueued.append
    )
    assert (second["dispatch_action"], second["dispatch_reason"]) == ("skipped", "issue_run_active")
    assert len(_workflow_runs(factory)) == 1 and len(temporal.started) == 1


def test_a_replayed_governed_delivery_starts_nothing(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    temporal: _FakeTemporalClient,
) -> None:
    connection_id = _seed_connection(factory, project_id)
    first = _seed_delivery(factory, connection_id)
    process_webhook(factory, first, connections=connections, payload=_payload())
    # The same signed body arriving again under another delivery id.
    replay = _seed_delivery(factory, connection_id, delivery_id="gh-9")

    result = process_webhook(factory, replay, connections=connections, payload=_payload())

    assert (result["dispatch_action"], result["dispatch_reason"]) == (
        "skipped",
        "already_dispatched",
    )
    assert len(temporal.started) == 1


def test_a_governed_dispatch_without_a_linked_task_creates_it(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    temporal: _FakeTemporalClient,
) -> None:
    """#171 criterion 2: no board task linked yet, so dispatch creates the task and
    its PMTaskLink before the run, and the run is for that task."""
    from scf_db.models.pm import PMTaskLink

    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id)
    with factory() as session:
        connection = session.get(PMConnection, connection_id)
        delivery = session.get(PMWebhookDelivery, row_id)
        assert connection is not None and delivery is not None
        session.expunge_all()

    outcome = maybe_dispatch(
        factory,
        connection=connection,
        delivery=delivery,
        payload=_payload(),
        connections=connections,
    )

    assert outcome.action == "dispatched"
    with factory() as session:
        [link] = session.execute(select(PMTaskLink)).scalars().all()
        assert link.external_id == "42"
    [run] = _workflow_runs(factory)
    assert run.task_id == link.forge_task_id


def test_a_governed_dispatch_for_a_task_on_another_repository_is_skipped(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    temporal: _FakeTemporalClient,
) -> None:
    """A linked task that already names another repository is refused, not run against two."""
    from scf_db.models.pm import PMTaskLink

    connection_id = _seed_connection(factory, project_id)
    with factory() as session:
        task = Task(
            workspace_id=WS_ID,
            project_id=project_id,
            key="CORE-77",
            title="elsewhere",
            repo_targets=[{"repo": "acme/other", "base_branch": "main"}],
        )
        session.add(task)
        session.flush()
        session.add(
            PMTaskLink(
                workspace_id=WS_ID,
                connection_id=connection_id,
                forge_task_id=task.id,
                provider=DbPMProvider.GITHUB_ISSUES,
                external_id="42",
                external_key="acme/widgets#42",
                external_url="https://github.com/acme/widgets/issues/42",
            )
        )
        session.commit()
    row_id = _seed_delivery(factory, connection_id)
    with factory() as session:
        connection = session.get(PMConnection, connection_id)
        delivery = session.get(PMWebhookDelivery, row_id)
        assert connection is not None and delivery is not None
        session.expunge_all()

    outcome = maybe_dispatch(
        factory,
        connection=connection,
        delivery=delivery,
        payload=_payload(),
        connections=connections,
    )

    assert (outcome.action, outcome.reason) == ("skipped", "task_repository_mismatch")
    assert temporal.started == [] and _workflow_runs(factory) == []


# --------------------------------------------------------------------------- #
# Governed write-back: the workflow's states reach the issue (#278)           #
# --------------------------------------------------------------------------- #

PR_URL = "https://github.com/acme/widgets/pull/7"


def _governed_run(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> uuid.UUID:
    """An issue dispatched to a governed run; the worker's write-back uses ``connections``."""
    from scf_worker.tasks import pm_dispatch

    monkeypatch.setattr(pm_dispatch, "_connection_service", lambda _factory: connections)
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id)
    result = process_webhook(factory, row_id, connections=connections, payload=_payload())
    assert result["dispatch_action"] == "dispatched"
    return uuid.UUID(result["run_id"])


def _reach(factory: sessionmaker[Session], run_id: uuid.UUID, state: str) -> None:
    """The worker records the run entering ``state``, as ``forge.persist_transition`` does."""
    import asyncio

    from scf_worker.temporal_main import SessionPerCallStore
    from scf_workflow.temporal.activities import WorkflowActivities
    from scf_workflow.temporal.payloads import TransitionRecord

    store = SessionPerCallStore(factory)
    record = TransitionRecord(
        workflow_run_id=run_id,
        workspace_id=WS_ID,
        from_state=store.get(run_id).current_state,
        to_state=state,
        event="test",
        idempotency_key=f"{run_id}:{state}",
    )
    asyncio.run(WorkflowActivities(store=store).persist_transition(record))


def _record_pull_request(factory: sessionmaker[Session], run_id: uuid.UUID) -> None:
    from scf_db.models.runs import WorkflowRun as DbWorkflowRun

    with factory() as session:
        row = session.get(DbWorkflowRun, run_id)
        assert row is not None
        row.context = {**row.context, "pull_request": {"number": 7, "url": PR_URL}}
        session.commit()


def test_a_governed_run_reports_each_terminal_state_once_with_its_pull_request(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    temporal: _FakeTemporalClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#278 criterion 1: the issue hears the workflow's state and its pull request,
    once for each state the run stops or ends in; the states in between say nothing."""
    from scf_worker.tasks.pm_dispatch import post_workflow_writeback
    from scf_worker.temporal_main import SessionPerCallStore

    run_id = _governed_run(factory, connections, project_id, monkeypatch)
    _reach(factory, run_id, "spec_review")
    assert fake_adapter.comments == [], "a run waiting at its spec gate has not finished"

    _record_pull_request(factory, run_id)
    _reach(factory, run_id, "awaiting_review")

    [(external_id, body)] = fake_adapter.comments
    assert external_id == "42"
    assert "awaiting_review" in body
    assert PR_URL in body
    assert f"<!-- forge:run:{run_id} -->" in body
    # The pull request opened: the connection's mapped transition, once.
    assert fake_adapter.transitions == [("42", StatusCategory.started)]

    # A redelivered transition, or a second write-back for the same state, posts nothing.
    _reach(factory, run_id, "awaiting_review")
    again = post_workflow_writeback(
        factory, SessionPerCallStore(factory).get(run_id), connections=connections
    )
    assert again == "already_recorded"
    assert len(fake_adapter.comments) == 1

    _reach(factory, run_id, "merged")
    _reach(factory, run_id, "closed")

    assert len(fake_adapter.comments) == 2
    closed = fake_adapter.comments[1][1]
    assert "closed" in closed and PR_URL in closed
    assert len(fake_adapter.transitions) == 1, "the pull request opened once"
    [run] = _workflow_runs(factory)
    assert {state: r["status"] for state, r in run.context["tracker_writeback"].items()} == {
        "awaiting_review": "posted",
        "closed": "posted",
    }


def test_a_tracker_outage_never_changes_a_governed_runs_outcome(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    temporal: _FakeTemporalClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _governed_run(factory, connections, project_id, monkeypatch)
    fake_adapter.raise_on_comment = RuntimeError("tracker down")

    _reach(factory, run_id, "failed")

    [run] = _workflow_runs(factory)
    assert (run.current_state, run.status) == ("failed", DbRunStatus.FAILED)
    assert run.context["tracker_writeback"]["failed"]["status"] == "failed"
    assert "tracker down" in run.context["tracker_writeback"]["failed"]["error"]


def test_a_governed_run_without_a_tracker_writes_nothing_back(
    factory: sessionmaker[Session], project_id: uuid.UUID
) -> None:
    """A run started through the API names no issue: nothing is posted or recorded."""
    from scf_db.models.runs import WorkflowRun as DbWorkflowRun
    from scf_worker.tasks.pm_dispatch import post_workflow_writeback
    from scf_worker.temporal_main import SessionPerCallStore

    with factory() as session:
        task = Task(workspace_id=WS_ID, project_id=project_id, key="CORE-9", title="api run")
        session.add(task)
        session.flush()
        row = DbWorkflowRun(
            workspace_id=WS_ID,
            task_id=task.id,
            engine_backend="temporal",
            current_state="closed",
            context={"trigger": {"source": "api", "started_by": "user:1"}},
        )
        session.add(row)
        session.commit()
        run_id = row.id
    run = SessionPerCallStore(factory).get(run_id)

    # connections=object() would explode if the write-back tried to use it.
    assert post_workflow_writeback(factory, run, connections=object()) == "not_applicable"  # type: ignore[arg-type]
    [stored] = _workflow_runs(factory)
    assert "tracker_writeback" not in stored.context


# --------------------------------------------------------------------------- #
# Ungoverned issue runs (#277)                                                #
# --------------------------------------------------------------------------- #


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from scf_api.settings import get_settings

    monkeypatch.setenv("FORGE_ENVIRONMENT", "production")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_production_without_the_durable_engine_refuses_an_issue_run(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    production: None,
) -> None:
    """#277 criterion 2: the direct path never runs a repository change in production."""
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id)
    enqueued: list[AgentObjective] = []

    result = process_webhook(
        factory, row_id, connections=connections, payload=_payload(), enqueue_run=enqueued.append
    )

    assert (result["dispatch_action"], result["dispatch_reason"]) == (
        "skipped",
        "ungoverned_run_refused",
    )
    assert enqueued == [] and _runs(factory) == []


def test_a_durable_engine_that_cannot_start_an_issue_run_skips_it(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    project_id: uuid.UUID,
    fake_adapter: FakeGitHubAdapter,
    temporal: _FakeTemporalClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#277 criterion 2: Temporal selected but unreachable never falls back to the
    direct path."""

    async def unreachable(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(temporal, "start_workflow", unreachable)
    connection_id = _seed_connection(factory, project_id)
    row_id = _seed_delivery(factory, connection_id)
    enqueued: list[AgentObjective] = []

    result = process_webhook(
        factory, row_id, connections=connections, payload=_payload(), enqueue_run=enqueued.append
    )

    assert (result["dispatch_action"], result["dispatch_reason"]) == (
        "skipped",
        "durable_engine_unavailable",
    )
    assert enqueued == [] and _runs(factory) == []
