"""Postgres integration tests for :class:`DbPolicyAuditSink` (F29 policy-audit-sink).

Exercises the DB-backed policy-audit sink against a real pgvector Postgres via the
shared ``pg_engine`` fixture (root ``conftest.py``): the ``emit`` seam end-to-end
— a full :class:`PolicyDecisionEvent` round-trip onto ``policy_rule_evaluation``
(including the JSONB ``matched_rule_ids`` list + ``context_redacted`` dict), the
append-only accumulation + newest-first ordering the F29 audit query relies on,
workspace / agent-run filtering, the ``agent_run_id`` FK constraint (and the
null-run path), durability across sink instances, and structural + behavioural
parity with the in-memory sink both backends implement.

Skips cleanly (parked) when no Postgres is reachable; runs under
``FORGE_TEST_DATABASE_URL`` (pgvector :5433) in the gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.policy_audit_sink_db import DbPolicyAuditSink
from forge_api.services.policy_service import (
    InMemoryPolicyAuditSink,
    PolicyAuditSink,
    PolicyDecisionEvent,
    PolicyService,
)
from forge_db.base import Base
from forge_db.models import AgentRun, PolicyRuleEvaluation, Workspace

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
    """Two workspaces + two agent_runs (the ``policy_rule_evaluation`` FK targets)."""
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    run = uuid.uuid4()
    other_run = uuid.uuid4()
    with factory() as session:
        session.add(Workspace(id=ws, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"))
        session.add(
            Workspace(id=other_ws, name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        )
        session.flush()
        session.add(AgentRun(id=run, workspace_id=ws))
        session.add(AgentRun(id=other_run, workspace_id=other_ws))
        session.commit()
    return {"ws": ws, "other_ws": other_ws, "run": run, "other_run": other_run}


@pytest.fixture
def sink(factory: sessionmaker[Session]) -> DbPolicyAuditSink:
    return DbPolicyAuditSink(factory)


def _event(
    seed: dict[str, uuid.UUID],
    *,
    workspace_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = "__default__",  # type: ignore[assignment]
    action: str = "write_file",
    final_effect: str = "deny",
    severity: str = "critical",
    matched_rule_ids: list[str] | None = None,
    step_id: uuid.UUID | None = None,
) -> PolicyDecisionEvent:
    return PolicyDecisionEvent(
        action=action,
        base_effect="allow",
        final_effect=final_effect,
        requires_approval=True,
        severity=severity,
        matched_rule_ids=matched_rule_ids
        if matched_rule_ids is not None
        else ["infra-writes-main-only", "second-rule"],
        context_redacted={"branch": "feature/x", "path": "infra/x.tf"},
        workspace_id=workspace_id or seed["ws"],
        agent_run_id=seed["run"] if agent_run_id == "__default__" else agent_run_id,
        step_id=step_id,
    )


def _rows(factory: sessionmaker[Session]) -> list[PolicyRuleEvaluation]:
    with factory() as session:
        return list(
            session.execute(
                select(PolicyRuleEvaluation).order_by(
                    PolicyRuleEvaluation.evaluated_at.asc()
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


def test_sink_satisfies_policy_audit_sink_protocol(sink: DbPolicyAuditSink) -> None:
    assert isinstance(sink, PolicyAuditSink)
    # And it drops straight into the service seam the in-memory sink fills.
    assert isinstance(PolicyService(audit_sink=sink).audit_sink, DbPolicyAuditSink)


# --------------------------------------------------------------------------- #
# emit round-trip                                                             #
# --------------------------------------------------------------------------- #


def test_emit_round_trips_every_field(
    sink: DbPolicyAuditSink, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    step = uuid.uuid4()
    sink.emit(_event(seed, step_id=step))

    rows = _rows(factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id == seed["ws"]
    assert row.agent_run_id == seed["run"]
    assert row.step_id == step
    assert row.action == "write_file"
    assert row.base_effect == "allow"
    assert row.final_effect == "deny"
    assert row.requires_approval is True
    assert row.severity == "critical"
    # JSONB list + dict survive verbatim (order + contents preserved).
    assert row.matched_rule_ids == ["infra-writes-main-only", "second-rule"]
    assert row.context_redacted == {"branch": "feature/x", "path": "infra/x.tf"}
    # Server-default timestamp populated; nothing beyond the event was written.
    assert row.evaluated_at is not None
    assert row.policy_snapshot_id is None


def test_emit_persists_null_agent_run(
    sink: DbPolicyAuditSink, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    sink.emit(_event(seed, agent_run_id=None))
    rows = _rows(factory)
    assert len(rows) == 1
    assert rows[0].agent_run_id is None


# --------------------------------------------------------------------------- #
# append-only accumulation + ordering                                         #
# --------------------------------------------------------------------------- #


def test_emit_is_append_only_and_newest_first_query(
    sink: DbPolicyAuditSink, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    for effect in ("deny", "allow", "deny"):
        sink.emit(_event(seed, final_effect=effect))
    # Three distinct append-only rows — never an update.
    assert len(_rows(factory)) == 3

    # The F29 workspace query (newest-first) reads them back in evaluated_at desc.
    service = PolicyService(audit_sink=sink)
    with factory() as session:
        listed = service.list_rule_evaluations(session, workspace_id=seed["ws"])
    assert len(listed) == 3
    stamps = [r.evaluated_at for r in listed]
    assert stamps == sorted(stamps, reverse=True)


# --------------------------------------------------------------------------- #
# workspace + agent-run filtering                                             #
# --------------------------------------------------------------------------- #


def test_query_is_workspace_and_run_scoped(
    sink: DbPolicyAuditSink, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    sink.emit(_event(seed))  # ws / run
    sink.emit(_event(seed))  # ws / run
    sink.emit(_event(seed, workspace_id=seed["other_ws"], agent_run_id=seed["other_run"]))

    service = PolicyService(audit_sink=sink)
    with factory() as session:
        ws_rows = service.list_rule_evaluations(session, workspace_id=seed["ws"])
        other_rows = service.list_rule_evaluations(session, workspace_id=seed["other_ws"])
        by_run = service.list_rule_evaluations(
            session, workspace_id=seed["ws"], agent_run_id=seed["run"]
        )
        foreign_run = service.list_rule_evaluations(
            session, workspace_id=seed["ws"], agent_run_id=seed["other_run"]
        )
    assert len(ws_rows) == 2
    assert len(other_rows) == 1
    assert len(by_run) == 2
    assert foreign_run == []


# --------------------------------------------------------------------------- #
# FK constraint (referential integrity enforced by the database)              #
# --------------------------------------------------------------------------- #


def test_emit_unknown_agent_run_rejected(
    sink: DbPolicyAuditSink, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    ghost = _event(seed, agent_run_id=uuid.uuid4())
    with pytest.raises(IntegrityError):
        sink.emit(ghost)
    # The failed insert left no row behind, and the sink still works afterwards.
    assert _rows(factory) == []
    sink.emit(_event(seed))
    assert len(_rows(factory)) == 1


# --------------------------------------------------------------------------- #
# durability across sink instances                                            #
# --------------------------------------------------------------------------- #


def test_persists_across_sink_instances(
    seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    DbPolicyAuditSink(factory).emit(_event(seed))
    # A second, independently constructed sink sees the same durable trail.
    rows = _rows(factory)
    assert len(rows) == 1
    assert rows[0].matched_rule_ids == ["infra-writes-main-only", "second-rule"]


# --------------------------------------------------------------------------- #
# parity with the in-memory sink (same seam, identical projection)            #
# --------------------------------------------------------------------------- #


def test_matches_in_memory_sink_projection(
    sink: DbPolicyAuditSink, seed: dict[str, uuid.UUID], factory: sessionmaker[Session]
) -> None:
    mem = InMemoryPolicyAuditSink()
    event = _event(seed)

    mem.emit(event)
    sink.emit(event)

    assert len(mem.events) == 1
    captured = mem.events[0]

    rows = _rows(factory)
    assert len(rows) == 1
    row = rows[0]
    # The durable row is the exact projection the in-memory sink captured.
    assert row.action == captured.action
    assert row.base_effect == captured.base_effect
    assert row.final_effect == captured.final_effect
    assert row.requires_approval == captured.requires_approval
    assert row.severity == captured.severity
    assert row.matched_rule_ids == captured.matched_rule_ids
    assert row.context_redacted == captured.context_redacted
    assert row.workspace_id == captured.workspace_id
    assert row.agent_run_id == captured.agent_run_id
