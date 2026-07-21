"""Worker board-write path for PM webhooks (``forge.pm.process_webhook``).

The webhook intake (``pm_service.receive_webhook``) verifies, dedupes, and
persists a delivery, then enqueues this task with the delivery row id. These
tests prove the worker side actually lands the board write:

* the task loads the persisted delivery, re-fetches the changed issue through
  the adapter built by the registry seam (a fake adapter is injected via
  ``build_adapter``; the real provider adapters stay untouched), and upserts a
  board task plus a durable ``pm_task_link`` row scoped to the connection's
  workspace (no cross-tenant leakage);
* redelivery is a no-op twice over: re-running the *same* delivery row is an
  early-returned no-op (row already marked processed), and a *new* delivery
  carrying identical content is echo-suppressed by the sync engine's content
  hash — board row count and ``updated_at`` stay stable in both cases.

Hermetic: in-memory SQLite (StaticPool), no Celery broker, no sockets.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.crypto import HmacAeadCipher
from forge_api.auth.vault import SecretVault
from forge_api.observability.audit import AuditLog
from forge_api.services import pm_service as pm_service_module
from forge_api.services.pm_service import PMConnectionService
from forge_contracts.enums import Direction
from forge_contracts.pm import (
    ExternalTask,
    PMConnectionConfig,
    PMProvider,
    StatusCategory,
)
from forge_db.base import Base
from forge_db.models import Project, Task, Workspace
from forge_db.models.enums import PMDeliveryStatus
from forge_db.models.enums import PMProvider as DbPMProvider
from forge_db.models.pm import PMTaskLink, PMWebhookDelivery
from forge_integrations.pm.errors import ExternalNotFound, RateLimitError
from forge_worker.celery_app import celery_app
from forge_worker.reliability import ForgeTask as ForgeReliableTask
from forge_worker.reliability import TransientError
from forge_worker.tasks import pm_sync as pm_sync_module
from forge_worker.tasks.pm_sync import PM_SYNC_TASK, process_webhook

WS_ID = uuid.uuid4()
OTHER_WS_ID = uuid.uuid4()


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


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
        session.add(Workspace(id=OTHER_WS_ID, name="Other", slug="other"))
        session.flush()
        project = Project(workspace_id=WS_ID, name="Core", key="CORE")
        session.add(project)
        session.commit()
        return project.id


class FakeAdapter:
    """A controllable inbound-path PMAdapter registered through the registry seam."""

    provider = PMProvider.linear

    def __init__(self) -> None:
        self.external: ExternalTask | None = None
        self.fetch_calls: list[str] = []
        #: When set, ``fetch_external`` raises this instead of returning — models a
        #: provider fault (transient or permanent) so the failure path is testable.
        self.raise_on_fetch: Exception | None = None

    # --- mapping (pure) ---
    def map_status(self, value: str, direction: Direction) -> str:
        return StatusCategory.started.value

    def map_priority(self, value: str, direction: Direction) -> str:
        return value

    def map_fields(self, fields: dict, direction: Direction) -> dict:
        return dict(fields)

    # --- external I/O (only the inbound fetch is legal here) ---
    async def fetch_external(self, external_id: str) -> ExternalTask:
        self.fetch_calls.append(external_id)
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        assert self.external is not None, "fake adapter has no external staged"
        return self.external


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    """Register a fake adapter through the ``build_adapter`` registry seam."""
    adapter = FakeAdapter()

    def _fake_build_adapter(
        provider: object, transport: object, ctx: object, *, auth_header: str | None = None
    ) -> FakeAdapter:
        return adapter

    # ``pm_service`` binds ``build_adapter`` at import time, so patch both the
    # registry function and the already-bound name.
    monkeypatch.setattr("forge_integrations.pm.registry.build_adapter", _fake_build_adapter)
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


@pytest.fixture
def connection_id(connections: PMConnectionService, project_id: uuid.UUID) -> uuid.UUID:
    cfg = PMConnectionConfig(
        provider=PMProvider.linear,
        name="Linear",
        project_id=project_id,
        external_project_key="ENG",
        auth_type="api_token",
        api_token="tok",
    )
    return connections.create(WS_ID, cfg).id


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _seed_delivery(
    factory: sessionmaker[Session],
    connection_id: uuid.UUID,
    *,
    delivery_id: str = "wh-1",
    event_type: str = "issue.updated",
    external_id: str | None = "ext-1",
) -> uuid.UUID:
    with factory() as session:
        row = PMWebhookDelivery(
            provider=DbPMProvider.LINEAR,
            connection_id=connection_id,
            delivery_id=delivery_id,
            event_type=event_type,
            external_id=external_id,
            payload_hash="deadbeef",
            signature_valid=True,
            received_at=datetime.now(UTC),
            status=PMDeliveryStatus.RECEIVED,
        )
        session.add(row)
        session.commit()
        return row.id


def _external(title: str = "Add pagination", description: str = "body") -> ExternalTask:
    return ExternalTask(
        provider=PMProvider.linear,
        external_id="ext-1",
        external_key="ENG-1",
        url="https://linear.app/acme/issue/ENG-1",
        title=title,
        description_md=description,
        status_name="In Progress",
        status_category=StatusCategory.started,
        priority_token="high",
        labels=["backend"],
        external_updated_at=datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC),
    )


def _board_state(factory: sessionmaker[Session]) -> tuple[list[Any], list[Any]]:
    with factory() as session:
        tasks = session.execute(select(Task)).scalars().all()
        links = session.execute(select(PMTaskLink)).scalars().all()
        for row in (*tasks, *links):
            session.expunge(row)
        return list(tasks), list(links)


def _delivery(factory: sessionmaker[Session], row_id: uuid.UUID) -> PMWebhookDelivery:
    with factory() as session:
        row = session.get(PMWebhookDelivery, row_id)
        assert row is not None
        session.expunge(row)
        return row


def _seed_other_tenant_link(
    factory: sessionmaker[Session], *, external_id: str = "ext-1"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a full OTHER_WS_ID task + PM link carrying the *same* external_id.

    Under a *different* connection (outside WS_ID's scope), so the WS_ID sync must
    never read or overwrite it despite the id collision. Returns (task_id, link_id).
    """
    with factory() as session:
        project = Project(workspace_id=OTHER_WS_ID, name="OtherCore", key="OCORE")
        session.add(project)
        session.flush()
        task = Task(
            workspace_id=OTHER_WS_ID, project_id=project.id, key="OCORE-1", title="Untouchable"
        )
        session.add(task)
        session.flush()
        link = PMTaskLink(
            workspace_id=OTHER_WS_ID,
            connection_id=uuid.uuid4(),  # a foreign connection, outside WS_ID's scope
            forge_task_id=task.id,
            provider=DbPMProvider.LINEAR,
            external_id=external_id,
            external_key="OCORE-1",
            external_url="https://linear.app/other/issue/OCORE-1",
        )
        session.add(link)
        session.commit()
        return task.id, link.id


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_process_webhook_fetches_and_upserts_board_task(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
    project_id: uuid.UUID,
) -> None:
    fake_adapter.external = _external()
    # A different tenant already holds a task + link under the *same* external_id
    # (via a foreign connection): the sync must not read or clobber it.
    other_task_id, other_link_id = _seed_other_tenant_link(factory, external_id="ext-1")
    row_id = _seed_delivery(factory, connection_id)

    result = process_webhook(factory, row_id, connections=connections)

    assert result["action"] == "created"
    assert fake_adapter.fetch_calls == ["ext-1"]

    tasks, links = _board_state(factory)
    # Multi-tenancy: the board write is scoped to the connection's workspace only.
    ws_tasks = [t for t in tasks if t.workspace_id == WS_ID]
    other_tasks = [t for t in tasks if t.workspace_id == OTHER_WS_ID]
    ws_links = [link for link in links if link.workspace_id == WS_ID]
    other_links = [link for link in links if link.workspace_id == OTHER_WS_ID]

    assert len(ws_tasks) == 1
    task = ws_tasks[0]
    assert task.project_id == project_id
    assert task.title == "Add pagination"
    assert task.description == "body"
    assert list(task.labels) == ["backend"]

    assert len(ws_links) == 1
    link = ws_links[0]
    assert link.connection_id == connection_id
    assert link.forge_task_id == task.id
    assert link.external_id == "ext-1"

    # The identically-keyed OTHER_WS_ID rows are untouched (no cross-tenant leak).
    assert [t.id for t in other_tasks] == [other_task_id]
    assert other_tasks[0].title == "Untouchable"
    assert [link.id for link in other_links] == [other_link_id]

    delivery = _delivery(factory, row_id)
    assert delivery.status == PMDeliveryStatus.PROCESSED
    assert delivery.processed_at is not None


def test_same_delivery_row_reprocessed_is_noop(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.external = _external()
    row_id = _seed_delivery(factory, connection_id)
    first = process_webhook(factory, row_id, connections=connections)
    assert first["action"] == "created"
    tasks_before, links_before = _board_state(factory)

    second = process_webhook(factory, row_id, connections=connections)

    assert second["action"] == "already_processed"
    assert fake_adapter.fetch_calls == ["ext-1"]  # no second re-fetch
    tasks_after, links_after = _board_state(factory)
    assert len(tasks_after) == len(tasks_before) == 1
    assert len(links_after) == len(links_before) == 1
    assert tasks_after[0].updated_at == tasks_before[0].updated_at


def test_redelivered_content_is_echo_suppressed(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.external = _external()
    first_id = _seed_delivery(factory, connection_id, delivery_id="wh-1")
    process_webhook(factory, first_id, connections=connections)
    tasks_before, _ = _board_state(factory)

    # A *distinct* delivery (provider retried) carrying identical content.
    second_id = _seed_delivery(factory, connection_id, delivery_id="wh-2")
    result = process_webhook(factory, second_id, connections=connections)

    assert result["action"] == "skipped_echo"
    tasks_after, links_after = _board_state(factory)
    assert len(tasks_after) == 1
    assert len(links_after) == 1
    assert tasks_after[0].updated_at == tasks_before[0].updated_at
    assert _delivery(factory, second_id).status == PMDeliveryStatus.ECHO_SUPPRESSED


def test_changed_external_content_updates_in_place(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.external = _external()
    process_webhook(
        factory, _seed_delivery(factory, connection_id, delivery_id="wh-1"), connections=connections
    )

    fake_adapter.external = _external(title="Add pagination v2")
    result = process_webhook(
        factory, _seed_delivery(factory, connection_id, delivery_id="wh-2"), connections=connections
    )

    assert result["action"] == "updated"
    tasks, links = _board_state(factory)
    assert len(tasks) == 1
    assert len(links) == 1
    assert tasks[0].title == "Add pagination v2"
    assert tasks[0].workspace_id == WS_ID


def test_deleted_event_is_skipped(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.external = _external()
    row_id = _seed_delivery(factory, connection_id, event_type="issue.deleted")

    result = process_webhook(factory, row_id, connections=connections)

    assert result["action"] == "skipped_delete"
    assert fake_adapter.fetch_calls == []
    tasks, links = _board_state(factory)
    assert tasks == []
    assert links == []
    assert _delivery(factory, row_id).status == PMDeliveryStatus.SKIPPED


def test_missing_connection_is_skipped(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
) -> None:
    row_id = _seed_delivery(factory, connection_id)
    with factory() as session:
        row = session.get(PMWebhookDelivery, row_id)
        assert row is not None
        row.connection_id = None  # connection deleted post-intake (ON DELETE SET NULL)
        session.commit()

    result = process_webhook(factory, row_id, connections=connections)

    assert result["action"] == "skipped"
    assert _board_state(factory) == ([], [])
    assert _delivery(factory, row_id).status == PMDeliveryStatus.SKIPPED


def test_permanent_fetch_failure_lands_error_and_reraises(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
) -> None:
    # A *permanent* provider fault (the issue does not exist): fail fast to ERROR,
    # re-raise as-is (never wrapped as TransientError), never touch the board.
    fake_adapter.raise_on_fetch = ExternalNotFound("x" * 2500)  # long -> exercises truncation
    row_id = _seed_delivery(factory, connection_id)

    with pytest.raises(ExternalNotFound):
        process_webhook(factory, row_id, connections=connections)

    assert fake_adapter.fetch_calls == ["ext-1"]  # tried once, not retried
    assert _board_state(factory) == ([], [])  # no board write
    delivery = _delivery(factory, row_id)
    assert delivery.status == PMDeliveryStatus.ERROR
    assert delivery.error is not None
    assert len(delivery.error) == 2000  # truncated error text
    assert delivery.processed_at is not None


def test_transient_fetch_failure_is_retried_by_reliability_base(
    factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection_id: uuid.UUID,
    fake_adapter: FakeAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A *transient* provider fault (rate-limit): the ForgeTask base auto-retries.
    # Drive the real registered task in eager mode and assert the authoritative
    # re-fetch is attempted more than once (initial + backoff retries).
    fake_adapter.raise_on_fetch = RateLimitError("rate limited")
    row_id = _seed_delivery(factory, connection_id)

    # Point the production seams at the hermetic test factory + connections.
    monkeypatch.setattr(pm_sync_module, "_session_factory", lambda: factory)
    monkeypatch.setattr(pm_sync_module, "_connection_service", lambda _factory: connections)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", False)

    result = celery_app.tasks[PM_SYNC_TASK].apply(args=[str(row_id)])

    assert result.failed()  # retry budget spent -> terminal failure
    # A retry *was* attempted: initial call + ForgeTask.max_retries re-fetches.
    assert len(fake_adapter.fetch_calls) == ForgeReliableTask.max_retries + 1
    assert len(fake_adapter.fetch_calls) > 1
    assert _delivery(factory, row_id).status == PMDeliveryStatus.ERROR


def test_task_registered_in_celery_app() -> None:
    assert PM_SYNC_TASK == "forge.pm.process_webhook"
    assert PM_SYNC_TASK in celery_app.tasks
    assert "forge_worker.tasks.pm_sync" in celery_app.conf.include
    # Registered on the reliability base: acks_late + auto-retry of TransientError
    # (so transient provider blips retry with backoff instead of being dropped).
    task = celery_app.tasks[PM_SYNC_TASK]
    assert isinstance(task, ForgeReliableTask)
    assert TransientError in task.autoretry_for
    assert task.acks_late is True
