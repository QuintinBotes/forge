"""Spec L §6a: gates the worker opens or changes reach ``/ws``.

The worker has no ``/ws`` sockets of its own; it publishes onto the Redis
channel the API relays (``forge:rt:<workspace>``, the broadcaster's JSON). A
fake Redis records what was published.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import forge_worker.approval_activity as approval_activity
from forge_approval.delegation import DelegationDirectory, DelegationEntry
from forge_contracts import RealtimeEvent, RealtimeEventType
from forge_db.base import Base
from forge_db.models import (
    ApprovalGate,
    ApprovalRequest,
    ApprovalStatus,
    AuditLog,
    Project,
    Task,
    Workspace,
)
from forge_worker.approval_activity import make_create_approval_fn
from forge_worker.tasks import approvals as approvals_tasks
from forge_workflow.temporal.payloads import ApprovalInput

WS = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
AWAY = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
DELEGATE = uuid.UUID("00000000-0000-0000-0000-0000000000f3")
RELAY = "redis://relay.test:6379/0"


class FakeRedis:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.threads: list[int] = []
        self.down = False

    def from_url(self, url: str, **_: Any) -> FakeRedis:
        self.urls.append(url)
        return self

    def publish(self, channel: str, data: str) -> int:
        self.threads.append(threading.get_ident())
        if self.down:
            raise redis.ConnectionError("relay unreachable")
        self.published.append((channel, data))
        return 1

    def close(self) -> None:
        return None

    def events(self) -> list[RealtimeEvent]:
        assert {channel for channel, _ in self.published} <= {f"forge:rt:{WS}"}
        return [RealtimeEvent.model_validate_json(data) for _, data in self.published]


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", fake.from_url)
    return fake


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
    yield sf
    engine.dispose()


# --------------------------------------------------------------------------- #
# forge.create_approval                                                         #
# --------------------------------------------------------------------------- #


def _seed_task(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as s:
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(workspace_id=WS, project_id=project.id, key="TASK-1", title="durable gate")
        s.add(task)
        s.commit()
        return task.id


async def test_the_activity_announces_the_gate_it_opens(
    factory: sessionmaker[Session], fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval_activity, "relay_url", lambda: RELAY)
    task_id = _seed_task(factory)
    run_id = uuid.uuid4()

    request_id = await make_create_approval_fn(factory)(
        ApprovalInput(
            workflow_run_id=run_id, workspace_id=WS, task_id=task_id, gate="spec", summary="s"
        )
    )

    assert fake_redis.urls == [RELAY]
    assert fake_redis.threads != [threading.get_ident()]  # published off the event loop
    (event,) = fake_redis.events()
    assert (event.type, event.approval_id, event.workspace_id) == (
        RealtimeEventType.APPROVAL_REQUESTED,
        request_id,
        WS,
    )
    assert event.payload["gate"] == "spec"
    assert event.payload["title"] == "durable gate"
    assert event.payload["workflow_run_id"] == str(run_id)
    assert event.payload["task_id"] == str(task_id)
    assert "gate_payload" not in event.payload  # the summary stays behind /context
    assert (event.observed_by, event.enforceable) == ("forge", True)


async def test_the_activity_publishes_nothing_on_the_in_process_backend(
    factory: sessionmaker[Session], fake_redis: FakeRedis
) -> None:
    # FORGE_REALTIME_BACKEND is unset in tests: no other process could relay it.
    await make_create_approval_fn(factory)(
        ApprovalInput(
            workflow_run_id=uuid.uuid4(), workspace_id=WS, task_id=_seed_task(factory), gate="plan"
        )
    )
    assert fake_redis.published == []


# --------------------------------------------------------------------------- #
# SLA sweep                                                                     #
# --------------------------------------------------------------------------- #

NOW = datetime.now(UTC)


def _gate(factory: sessionmaker[Session], *, created_ago: int, expires_in: int, **kw: Any) -> Any:
    with factory() as s:
        gate = ApprovalRequest(
            workspace_id=WS,
            gate=ApprovalGate.PR,
            status=ApprovalStatus.PENDING,
            subject_type="workflow_run",
            subject_id=uuid.uuid4(),
            summary="aging gate",
            created_at=NOW - timedelta(seconds=created_ago),
            expires_at=NOW + timedelta(seconds=expires_in),
            **kw,
        )
        s.add(gate)
        s.commit()
        return gate.id


def test_the_sweep_returns_its_events_and_publishes_none_itself(
    factory: sessionmaker[Session], fake_redis: FakeRedis
) -> None:
    expired = _gate(factory, created_ago=120, expires_in=-20)
    escalated = _gate(factory, created_ago=80, expires_in=20)
    routed = _gate(factory, created_ago=80, expires_in=20, payload={"assignee": str(AWAY)})
    away = DelegationDirectory(entries=[DelegationEntry(user_id=AWAY, delegate_id=DELEGATE)])

    with factory() as session:
        result = approvals_tasks.run_sla_sweep(session, now=NOW, delegation=away)

    assert fake_redis.published == []
    by_id = {e.approval_id: e for e in result.events}
    assert by_id[expired].type is RealtimeEventType.APPROVAL_DECIDED
    assert (
        by_id[expired].payload.items()
        >= {
            "status": "expired",
            "decided_by": "system",
            "surface": "system",
            "title": "aging gate",
        }.items()
    )
    assert by_id[escalated].type is RealtimeEventType.APPROVAL_UPDATED
    assert (
        by_id[escalated].payload.items()
        >= {
            "status": "pending",
            "reason": "escalated",
            "risk_level": "critical",
        }.items()
    )
    assert by_id[routed].type is RealtimeEventType.APPROVAL_UPDATED
    assert by_id[routed].payload["reason"] == "routed_to_delegate"
    with factory() as s:
        audit = {(row.action, row.details["surface"]) for row in s.scalars(select(AuditLog))}
    assert audit == {
        ("approval.expired", "system"),
        ("approval.escalated", "system"),
        ("approval.routed_to_delegate", "system"),
    }


def test_the_celery_task_publishes_only_after_the_commit(
    factory: sessionmaker[Session], fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _gate(factory, created_ago=120, expires_in=-20)
    order: list[str] = []

    class RecordingSession(Session):
        def commit(self) -> None:
            super().commit()
            order.append("commit")

    recording = sessionmaker(bind=factory.kw["bind"], class_=RecordingSession)
    monkeypatch.setattr(approvals_tasks, "create_session_factory", lambda: recording)
    monkeypatch.setattr(approvals_tasks, "relay_url", lambda: RELAY)
    real_publish = approvals_tasks.publish_sync

    def publish(url: str | None, event: RealtimeEvent) -> None:
        order.append("publish")
        real_publish(url, event)

    monkeypatch.setattr(approvals_tasks, "publish_sync", publish)

    assert approvals_tasks.expire_pending_approvals() == 1

    assert order == ["commit", "publish"]
    (event,) = fake_redis.events()
    assert (event.type, event.approval_id) == (RealtimeEventType.APPROVAL_DECIDED, expired)
    with factory() as s:
        status = s.scalars(select(ApprovalRequest.status).where(ApprovalRequest.id == expired))
        assert status.one() is ApprovalStatus.EXPIRED


# --------------------------------------------------------------------------- #
# Publishing is best effort                                                     #
# --------------------------------------------------------------------------- #


async def test_a_failing_publish_never_breaks_the_activity(
    factory: sessionmaker[Session], fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approval_activity, "relay_url", lambda: RELAY)
    fake_redis.down = True

    request_id = await make_create_approval_fn(factory)(
        ApprovalInput(
            workflow_run_id=uuid.uuid4(), workspace_id=WS, task_id=_seed_task(factory), gate="pr"
        )
    )

    assert fake_redis.threads and fake_redis.published == []  # it tried, and failed
    with factory() as s:
        assert s.get(ApprovalRequest, request_id) is not None


def test_a_failing_publish_never_breaks_the_sweep(
    factory: sessionmaker[Session], fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _gate(factory, created_ago=120, expires_in=-20)
    monkeypatch.setattr(approvals_tasks, "create_session_factory", lambda: factory)
    monkeypatch.setattr(approvals_tasks, "relay_url", lambda: RELAY)
    fake_redis.down = True

    assert approvals_tasks.expire_pending_approvals() == 1

    assert fake_redis.threads and fake_redis.published == []
    with factory() as s:
        assert s.get(ApprovalRequest, expired).status is ApprovalStatus.EXPIRED  # type: ignore[union-attr]


def test_an_event_that_cannot_be_built_never_blocks_expiry(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _gate(factory, created_ago=120, expires_in=-20)

    def broken(*_: Any, **__: Any) -> RealtimeEvent:
        raise ValueError("a row the event builder cannot read")

    monkeypatch.setattr(approvals_tasks.approval_events, "decided", broken)

    with factory() as session:
        result = approvals_tasks.run_sla_sweep(session, now=NOW)

    assert (result.expired, result.events) == (1, [])
    with factory() as s:
        assert s.get(ApprovalRequest, expired).status is ApprovalStatus.EXPIRED  # type: ignore[union-attr]
