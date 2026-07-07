"""Postgres integration tests for :class:`SqlAlchemyApprovalRepository` (F36).

Exercises the DB-backed approval repository against a real pgvector Postgres via
the shared ``pg_engine`` fixture (root ``conftest.py``): the async
``ApprovalRepository`` protocol end-to-end — a full ``ApprovalRequest`` round-trip
(every domain field, including the repository-only ``requested_actor`` /
``escalated`` and the ``gate_payload`` JSONB), workspace-scoped reads +
cross-workspace isolation, ``find_pending`` semantics, ``list`` filtering +
ordering, ``update`` (status transition, escalation flag, unknown-id
``ApprovalNotFoundError``), the append-only per-approver decision trail
(``add_decision`` + ``DuplicateDecisionError`` + ``decisions_for`` ordering), the
``uq_pending_gate`` storage-boundary constraint, durability across repository
instances, and structural conformance to the same protocol the in-memory store
implements. Skips cleanly (parked) when no Postgres is reachable; runs under
``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate.

Each behaviour mirrors the in-memory contract, so both backends are proven to
satisfy the same protocol identically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRecord,
    ApprovalRequest,
    GateStatus,
    GateType,
)
from forge_approval.repository import (
    ApprovalNotFoundError,
    ApprovalRepository,
    DuplicateDecisionError,
    InMemoryApprovalRepository,
)
from forge_db.base import Base
from forge_db.models import User, Workspace

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("pg_engine")]


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def seed(factory: sessionmaker[Session]) -> dict[str, uuid.UUID]:
    """A workspace + two reviewers, plus a second isolated workspace."""
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=ws, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        session.add(Workspace(id=other_ws, name="Other", slug=f"other-{uuid.uuid4().hex[:8]}"))
        session.flush()
        session.add(
            User(id=alice, workspace_id=ws, email=f"a-{alice.hex[:6]}@acme.dev", name="Alice")
        )
        session.add(User(id=bob, workspace_id=ws, email=f"b-{bob.hex[:6]}@acme.dev", name="Bob"))
        session.commit()
    return {"ws": ws, "other_ws": other_ws, "alice": alice, "bob": bob}


@pytest.fixture
def repo(factory: sessionmaker[Session]) -> SqlAlchemyApprovalRepository:
    return SqlAlchemyApprovalRepository(factory)


def _request(
    ws: uuid.UUID,
    *,
    gate: GateType = GateType.PR,
    status: GateStatus = GateStatus.PENDING,
    subject_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    risk_level: str = "info",
    requested_actor: str = "system",
    requested_at: datetime | None = None,
    **kw,
) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.uuid4(),
        workspace_id=ws,
        project_id=project_id,
        gate_type=gate,
        status=status,
        subject_type="workflow_run",
        subject_id=subject_id or uuid.uuid4(),
        risk_level=risk_level,  # type: ignore[arg-type]
        requested_actor=requested_actor,
        requested_at=requested_at or datetime.now(UTC),
        **kw,
    )


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_repo_satisfies_approval_repository_protocol(
    repo: SqlAlchemyApprovalRepository,
) -> None:
    assert isinstance(repo, ApprovalRepository)


# --------------------------------------------------------------------------- #
# add + get round-trip                                                        #
# --------------------------------------------------------------------------- #


async def test_add_then_get_round_trips_every_field(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    requested_at = datetime(2026, 7, 5, 12, 0, 0, 123456, tzinfo=UTC)
    expires_at = datetime(2026, 7, 5, 16, 0, 0, 654321, tzinfo=UTC)
    request = _request(
        ws,
        gate=GateType.DEPLOY,
        risk_level="critical",
        requested_actor=f"user:{seed['alice']}",
        requested_at=requested_at,
        project_id=uuid.uuid4(),
        required_approvals=2,
        title="Ship it",
        gate_payload={"env": "prod", "nested": {"k": [1, 2, 3]}},
        context_ref="s3://ctx/abc",
        requested_by=seed["alice"],
        escalated=True,
        expires_at=expires_at,
    )
    returned = await repo.add(request)
    # ``add`` returns the stored request verbatim (parity with the in-memory store).
    assert returned.id == request.id
    assert returned.requested_actor == f"user:{seed['alice']}"

    loaded = await repo.get(request.id, workspace_id=ws)
    assert loaded is not None
    assert loaded.id == request.id
    assert loaded.gate_type is GateType.DEPLOY
    assert loaded.status is GateStatus.PENDING
    assert loaded.subject_type == "workflow_run"
    assert loaded.subject_id == request.subject_id
    assert loaded.risk_level == "critical"
    assert loaded.required_approvals == 2
    assert loaded.title == "Ship it"
    assert loaded.gate_payload == {"env": "prod", "nested": {"k": [1, 2, 3]}}
    assert loaded.context_ref == "s3://ctx/abc"
    assert loaded.requested_by == seed["alice"]
    assert loaded.requested_actor == f"user:{seed['alice']}"
    assert loaded.escalated is True
    assert loaded.project_id == request.project_id
    assert loaded.requested_at == requested_at
    assert loaded.expires_at == expires_at


async def test_add_fills_requested_at_when_missing(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    request = _request(seed["ws"])
    request.requested_at = None
    returned = await repo.add(request)
    assert returned.requested_at is not None
    loaded = await repo.get(request.id, workspace_id=seed["ws"])
    assert loaded is not None and loaded.requested_at is not None


async def test_get_is_workspace_scoped(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    request = _request(seed["ws"])
    await repo.add(request)
    # Correct workspace: found. Foreign workspace: reads as absent (never a leak).
    assert await repo.get(request.id, workspace_id=seed["ws"]) is not None
    assert await repo.get(request.id, workspace_id=seed["other_ws"]) is None
    assert await repo.get(uuid.uuid4(), workspace_id=seed["ws"]) is None


# --------------------------------------------------------------------------- #
# find_pending                                                                #
# --------------------------------------------------------------------------- #


async def test_find_pending_matches_subject_gate_and_status(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    subject = uuid.uuid4()
    request = _request(ws, gate=GateType.PR, subject_id=subject)
    await repo.add(request)

    found = await repo.find_pending(
        workspace_id=ws,
        subject_type="workflow_run",
        subject_id=subject,
        gate_type=GateType.PR,
    )
    assert found is not None and found.id == request.id

    # Different gate type / subject / workspace -> no match.
    assert (
        await repo.find_pending(
            workspace_id=ws,
            subject_type="workflow_run",
            subject_id=subject,
            gate_type=GateType.SPEC,
        )
        is None
    )
    assert (
        await repo.find_pending(
            workspace_id=seed["other_ws"],
            subject_type="workflow_run",
            subject_id=subject,
            gate_type=GateType.PR,
        )
        is None
    )
    # A None subject id never matches (mirrors the in-memory guard).
    assert (
        await repo.find_pending(
            workspace_id=ws,
            subject_type="workflow_run",
            subject_id=None,
            gate_type=GateType.PR,
        )
        is None
    )


async def test_find_pending_ignores_resolved_gates(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    subject = uuid.uuid4()
    request = _request(ws, subject_id=subject)
    await repo.add(request)
    request.status = GateStatus.APPROVED
    await repo.update(request)
    assert (
        await repo.find_pending(
            workspace_id=ws,
            subject_type="workflow_run",
            subject_id=subject,
            gate_type=GateType.PR,
        )
        is None
    )


# --------------------------------------------------------------------------- #
# list: filtering + ordering                                                  #
# --------------------------------------------------------------------------- #


async def test_list_filters_and_orders_by_requested_at(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    proj = uuid.uuid4()
    base = datetime(2026, 7, 5, 9, 0, 0, tzinfo=UTC)
    r1 = _request(ws, gate=GateType.PR, project_id=proj, requested_at=base)
    r2 = _request(ws, gate=GateType.SPEC, requested_at=base + timedelta(minutes=1))
    r3 = _request(ws, gate=GateType.PR, project_id=proj, requested_at=base + timedelta(minutes=2))
    r3.status = GateStatus.APPROVED
    for r in (r2, r3, r1):  # insert out of order; list must sort by requested_at
        await repo.add(r)
    # Foreign-workspace row must never appear.
    await repo.add(_request(seed["other_ws"]))

    all_ws = await repo.list(workspace_id=ws)
    assert [r.id for r in all_ws] == [r1.id, r2.id, r3.id]

    assert [r.id for r in await repo.list(workspace_id=ws, status=GateStatus.PENDING)] == [
        r1.id,
        r2.id,
    ]
    assert [r.id for r in await repo.list(workspace_id=ws, gate_type=GateType.PR)] == [
        r1.id,
        r3.id,
    ]
    assert [r.id for r in await repo.list(workspace_id=ws, project_id=proj)] == [
        r1.id,
        r3.id,
    ]
    assert (await repo.list(workspace_id=ws, gate_type=GateType.PR, status=GateStatus.PENDING))[
        0
    ].id == r1.id
    assert await repo.list(workspace_id=uuid.uuid4()) == []


# --------------------------------------------------------------------------- #
# update                                                                      #
# --------------------------------------------------------------------------- #


async def test_update_persists_resolution(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    request = _request(ws)
    await repo.add(request)

    resolved_at = datetime(2026, 7, 5, 13, 0, 0, tzinfo=UTC)
    request.status = GateStatus.REJECTED
    request.resolver_user_id = seed["bob"]
    request.decision_note = "no thanks"
    request.resolved_at = resolved_at
    returned = await repo.update(request)
    assert returned.status is GateStatus.REJECTED

    loaded = await repo.get(request.id, workspace_id=ws)
    assert loaded is not None
    assert loaded.status is GateStatus.REJECTED
    assert loaded.resolver_user_id == seed["bob"]
    assert loaded.decision_note == "no thanks"
    assert loaded.resolved_at == resolved_at


async def test_update_persists_escalation_flag(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    request = _request(ws, risk_level="info")
    await repo.add(request)
    request.escalated = True
    request.risk_level = "critical"  # type: ignore[assignment]
    await repo.update(request)
    loaded = await repo.get(request.id, workspace_id=ws)
    assert loaded is not None
    assert loaded.escalated is True
    assert loaded.risk_level == "critical"


async def test_update_unknown_id_raises_not_found(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ghost = _request(seed["ws"])  # never added
    with pytest.raises(ApprovalNotFoundError):
        await repo.update(ghost)


# --------------------------------------------------------------------------- #
# decisions: append-only, one vote per approver                               #
# --------------------------------------------------------------------------- #


async def test_add_decision_and_decisions_for_ordering(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    request = _request(ws)
    await repo.add(request)

    first = await repo.add_decision(
        ApprovalDecisionRecord(
            approval_request_id=request.id,
            approver_user_id=seed["alice"],
            decision=ApprovalAction.APPROVE,
            note="lgtm",
            created_at=datetime(2026, 7, 5, 10, 0, 0, tzinfo=UTC),
        )
    )
    assert first.created_at is not None
    second = await repo.add_decision(
        ApprovalDecisionRecord(
            approval_request_id=request.id,
            approver_user_id=seed["bob"],
            decision=ApprovalAction.REQUEST_CHANGES,
            created_at=datetime(2026, 7, 5, 10, 5, 0, tzinfo=UTC),
        )
    )

    records = await repo.decisions_for(request.id)
    assert [r.approver_user_id for r in records] == [seed["alice"], seed["bob"]]
    assert records[0].decision is ApprovalAction.APPROVE
    assert records[0].note == "lgtm"
    assert records[1].decision is ApprovalAction.REQUEST_CHANGES
    assert records[1].note is None
    assert second.approver_user_id == seed["bob"]


async def test_add_decision_duplicate_approver_raises(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    request = _request(ws)
    await repo.add(request)

    vote = ApprovalDecisionRecord(
        approval_request_id=request.id,
        approver_user_id=seed["alice"],
        decision=ApprovalAction.APPROVE,
    )
    await repo.add_decision(vote)
    with pytest.raises(DuplicateDecisionError):
        await repo.add_decision(
            ApprovalDecisionRecord(
                approval_request_id=request.id,
                approver_user_id=seed["alice"],
                decision=ApprovalAction.REJECT,
            )
        )
    # The append-only trail is unchanged after the rejected duplicate.
    assert len(await repo.decisions_for(request.id)) == 1


async def test_decisions_for_unknown_request_is_empty(
    repo: SqlAlchemyApprovalRepository,
) -> None:
    assert await repo.decisions_for(uuid.uuid4()) == []


# --------------------------------------------------------------------------- #
# Storage-boundary constraint + durability                                    #
# --------------------------------------------------------------------------- #


async def test_pending_unique_blocks_duplicate_open_gate(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    """The DB ``uq_pending_gate`` guards against a second open gate per subject.

    The service dedupes via ``find_pending`` first; a *direct* duplicate insert
    raises at the storage boundary rather than silently succeeding.
    """
    ws = seed["ws"]
    subject = uuid.uuid4()
    await repo.add(_request(ws, gate=GateType.PR, subject_id=subject))
    with pytest.raises(IntegrityError):
        await repo.add(_request(ws, gate=GateType.PR, subject_id=subject))


async def test_persists_across_repository_instances(
    factory: sessionmaker[Session], seed: dict[str, uuid.UUID]
) -> None:
    first = SqlAlchemyApprovalRepository(factory)
    request = _request(seed["ws"], title="durable")
    await first.add(request)

    second = SqlAlchemyApprovalRepository(factory)
    loaded = await second.get(request.id, workspace_id=seed["ws"])
    assert loaded is not None and loaded.title == "durable"


# --------------------------------------------------------------------------- #
# Parity with the in-memory store (same protocol, identical behaviour)        #
# --------------------------------------------------------------------------- #


async def test_matches_in_memory_store_behaviour(
    repo: SqlAlchemyApprovalRepository, seed: dict[str, uuid.UUID]
) -> None:
    ws = seed["ws"]
    mem: InMemoryApprovalRepository = InMemoryApprovalRepository()

    reqs = [
        _request(ws, gate=GateType.PR, risk_level="warning"),
        _request(ws, gate=GateType.SPEC, risk_level="info"),
    ]
    for r in reqs:
        await repo.add(r.model_copy(deep=True))
        await mem.add(r.model_copy(deep=True))

    db_pending = await repo.list(workspace_id=ws, status=GateStatus.PENDING)
    mem_pending = await mem.list(workspace_id=ws, status=GateStatus.PENDING)
    assert {r.id for r in db_pending} == {r.id for r in mem_pending}
    assert {r.id for r in await repo.list(workspace_id=ws, gate_type=GateType.PR)} == {
        r.id for r in await mem.list(workspace_id=ws, gate_type=GateType.PR)
    }

    # find_pending agrees on both backends.
    subject = reqs[0].subject_id
    db_found = await repo.find_pending(
        workspace_id=ws, subject_type="workflow_run", subject_id=subject, gate_type=GateType.PR
    )
    mem_found = await mem.find_pending(
        workspace_id=ws, subject_type="workflow_run", subject_id=subject, gate_type=GateType.PR
    )
    assert (db_found is None) == (mem_found is None)
    assert db_found is not None and db_found.id == mem_found.id
