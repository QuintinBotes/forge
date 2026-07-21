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
from forge_worker.celery_app import celery_app
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
    row_id = _seed_delivery(factory, connection_id)

    result = process_webhook(factory, row_id, connections=connections)

    assert result["action"] == "created"
    assert fake_adapter.fetch_calls == ["ext-1"]

    tasks, links = _board_state(factory)
    assert len(tasks) == 1
    task = tasks[0]
    # Multi-tenancy: the board write is scoped to the connection's workspace.
    assert task.workspace_id == WS_ID
    assert task.project_id == project_id
    assert task.title == "Add pagination"
    assert task.description == "body"
    assert list(task.labels) == ["backend"]

    assert len(links) == 1
    link = links[0]
    assert link.workspace_id == WS_ID
    assert link.connection_id == connection_id
    assert link.forge_task_id == task.id
    assert link.external_id == "ext-1"

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


def test_task_registered_in_celery_app() -> None:
    assert PM_SYNC_TASK == "forge.pm.process_webhook"
    assert PM_SYNC_TASK in celery_app.tasks
    assert "forge_worker.tasks.pm_sync" in celery_app.conf.include
