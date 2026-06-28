"""F29 — conditional policy routes + service persistence/audit (ACs 16-18).

Route/service tests run against in-memory SQLite (StaticPool, shared across the
app worker thread); the DB-level immutability trigger is exercised separately
against the real Postgres ``pg_engine`` fixture.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.policy import get_policy_service
from forge_api.services.policy_service import InMemoryPolicyAuditSink, PolicyService
from forge_contracts import (
    Condition,
    ConditionalRule,
    ConditionGroup,
    ConditionOp,
    DecisionEffect,
    Policy,
    RuleEffect,
    ToolCall,
    WriteRules,
)
from forge_db.base import Base
from forge_db.models import PolicyRuleEvaluation, Workspace
from forge_policy import PolicyContext

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _infra_policy() -> Policy:
    return Policy(
        repo_id="github.com/org/api",
        schema_version=2,
        write_rules=WriteRules(allow=["app/**", "infra/**"], deny=["secrets/**"]),
        rules=[
            ConditionalRule(
                id="infra-writes-main-only",
                applies_to=["write_file"],
                when=ConditionGroup(
                    conditions=[
                        Condition(field="path", op=ConditionOp.MATCHES_GLOB, value="infra/**"),
                        Condition(field="branch", op=ConditionOp.NE, value="main"),
                    ]
                ),
                effect=RuleEffect.DENY,
                severity="critical",
                reason="infra/** may only be modified on the main branch.",
            )
        ],
    )


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.add(Workspace(id=OTHER_WS_ID, name="Other", slug="other"))
        session.commit()
    return factory


@pytest.fixture
def audit_sink() -> InMemoryPolicyAuditSink:
    return InMemoryPolicyAuditSink()


@pytest.fixture
def service(audit_sink: InMemoryPolicyAuditSink) -> PolicyService:
    return PolicyService(audit_sink=audit_sink)


@pytest.fixture
def client_factory(
    service: PolicyService, session_factory: sessionmaker[Session]
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(role: str = "admin", workspace_id: uuid.UUID = WS_ID) -> TestClient:
        app = create_app()
        principal = Principal(
            user_id=USER_ID,
            workspace_id=workspace_id,
            role=role,  # type: ignore[arg-type]
            email="pol-test@forge.local",
            auth_method="test",
            scopes=["*"],
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_policy_service] = lambda: service

        def _get_db() -> Iterator[Session]:
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _get_db
        tc = TestClient(app)
        clients.append(tc)
        return tc

    yield _build
    for tc in clients:
        tc.close()


# --------------------------------------------------------------------------- #
# AC16 — simulate                                                             #
# --------------------------------------------------------------------------- #


def _simulate_payload() -> dict:
    return {
        "action": {"tool": "write_file", "path": "infra/x.tf"},
        "context": {"branch": "feature/x"},
        "policy": _infra_policy().model_dump(mode="json"),
    }


def test_simulate_ok_no_persistence(
    client_factory: Callable[..., TestClient], session_factory: sessionmaker[Session]
) -> None:
    client = client_factory(role="admin")
    resp = client.post("/policy/simulate", json=_simulate_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"]["effect"] == "deny"
    assert body["base_effect"] == "allow"
    traces = {t["rule_id"]: t for t in body["traces"]}
    assert traces["infra-writes-main-only"]["matched"] is True
    # No row was persisted by a dry-run.
    with session_factory() as session:
        count = session.execute(select(PolicyRuleEvaluation)).scalars().all()
    assert count == []


def test_simulate_unauth_401() -> None:
    app = create_app()  # no auth override -> real auth dependency rejects
    with TestClient(app) as client:
        resp = client.post("/policy/simulate", json=_simulate_payload())
    assert resp.status_code == 401


def test_simulate_viewer_403(client_factory: Callable[..., TestClient]) -> None:
    client = client_factory(role="viewer")
    resp = client.post("/policy/simulate", json=_simulate_payload())
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# AC17 — evaluate_and_record persists exactly one append-only row + audit event #
# --------------------------------------------------------------------------- #


def test_evaluate_persists_rule_evaluation(
    service: PolicyService,
    audit_sink: InMemoryPolicyAuditSink,
    session_factory: sessionmaker[Session],
) -> None:
    policy = _infra_policy()
    context = PolicyContext(branch="feature/x", command="export TOKEN=secret && terraform")
    run_id = uuid.uuid4()
    with session_factory() as session:
        decision = service.evaluate_and_record(
            session,
            workspace_id=WS_ID,
            action=ToolCall(tool="write_file", path="infra/x.tf"),
            policy=policy,
            context=context,
            agent_run_id=run_id,
            step_id=uuid.uuid4(),
        )
        session.commit()
        assert decision.effect is DecisionEffect.DENY

        rows = session.execute(select(PolicyRuleEvaluation)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.final_effect == "deny"
        assert row.base_effect == "allow"
        assert row.matched_rule_ids == ["infra-writes-main-only"]
        assert row.severity == "critical"
        assert row.agent_run_id == run_id
        # context_redacted must not leak the command.
        assert "command" not in row.context_redacted
        assert row.context_redacted["branch"] == "feature/x"

    # Exactly one audit event, redacted (no command / raw args).
    assert len(audit_sink.events) == 1
    event = audit_sink.events[0]
    assert event.final_effect == "deny"
    assert event.matched_rule_ids == ["infra-writes-main-only"]
    assert "command" not in event.context_redacted

    # Append-only: a second identical call writes a SECOND row (never an update).
    with session_factory() as session:
        service.evaluate_and_record(
            session,
            workspace_id=WS_ID,
            action=ToolCall(tool="write_file", path="infra/x.tf"),
            policy=policy,
            context=context,
            agent_run_id=run_id,
        )
        session.commit()
    with session_factory() as session:
        rows = session.execute(select(PolicyRuleEvaluation)).scalars().all()
    assert len(rows) == 2


def test_no_row_when_no_conditional_rule_matches(
    service: PolicyService,
    audit_sink: InMemoryPolicyAuditSink,
    session_factory: sessionmaker[Session],
) -> None:
    policy = _infra_policy()
    with session_factory() as session:
        service.evaluate_and_record(
            session,
            workspace_id=WS_ID,
            action=ToolCall(tool="write_file", path="app/main.py"),
            policy=policy,
            context=PolicyContext(branch="feature/x"),
            agent_run_id=uuid.uuid4(),
        )
        session.commit()
        rows = session.execute(select(PolicyRuleEvaluation)).scalars().all()
    assert rows == []
    assert audit_sink.events == []


# --------------------------------------------------------------------------- #
# AC18 — rule-evaluations query is workspace-scoped                           #
# --------------------------------------------------------------------------- #


def _seed_eval(session: Session, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    session.add(
        PolicyRuleEvaluation(
            workspace_id=workspace_id,
            agent_run_id=run_id,
            action="write_file",
            base_effect="allow",
            final_effect="deny",
            requires_approval=False,
            severity="critical",
            matched_rule_ids=["infra-writes-main-only"],
            context_redacted={"branch": "feature/x"},
        )
    )


def test_rule_evaluations_query_scoped(
    client_factory: Callable[..., TestClient], session_factory: sessionmaker[Session]
) -> None:
    run_id = uuid.uuid4()
    with session_factory() as session:
        _seed_eval(session, WS_ID, run_id)
        _seed_eval(session, OTHER_WS_ID, uuid.uuid4())
        session.commit()

    client = client_factory(role="admin", workspace_id=WS_ID)
    resp = client.get("/policy/rule-evaluations")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["matched_rule_ids"] == ["infra-writes-main-only"]
    assert rows[0]["agent_run_id"] == str(run_id)

    # Filter by agent_run_id.
    resp = client.get("/policy/rule-evaluations", params={"agent_run_id": str(run_id)})
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # A run id from another workspace yields nothing for this workspace.
    other_client = client_factory(role="admin", workspace_id=OTHER_WS_ID)
    resp = other_client.get("/policy/rule-evaluations", params={"agent_run_id": str(run_id)})
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# Test endpoint — run a policy-as-code suite                                  #
# --------------------------------------------------------------------------- #


def test_test_endpoint_runs_inline_suite(client_factory: Callable[..., TestClient]) -> None:
    client = client_factory(role="admin")
    payload = {
        "policy": _infra_policy().model_dump(mode="json"),
        "suite": {
            "cases": [
                {
                    "name": "infra on feature branch is denied",
                    "context": {"branch": "feature/x"},
                    "tool_call": {"tool": "write_file", "path": "infra/x.tf"},
                    "expect_effect": "deny",
                    "expect_rule": "infra-writes-main-only",
                },
                {
                    "name": "infra on main is allowed",
                    "context": {"branch": "main"},
                    "tool_call": {"tool": "write_file", "path": "infra/x.tf"},
                    "expect_effect": "allow",
                },
            ]
        },
    }
    resp = client.post("/policy/test", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["passed"] == 2
    assert body["failed"] == 0


def test_test_endpoint_viewer_forbidden(client_factory: Callable[..., TestClient]) -> None:
    client = client_factory(role="viewer")
    resp = client.post(
        "/policy/test",
        json={"policy": _infra_policy().model_dump(mode="json"), "suite": {"cases": []}},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Immutability trigger (real Postgres) — F39 attach_immutability_trigger.     #
# --------------------------------------------------------------------------- #


def test_immutability_trigger_blocks_update(pg_engine) -> None:
    """A raw UPDATE/DELETE on policy_rule_evaluation is rejected at the DB level."""
    from sqlalchemy.exc import DatabaseError

    # Full metadata so the FK targets (workspace, agent_run) and the F39
    # immutability trigger exist in the session-private schema.
    Base.metadata.create_all(pg_engine)
    ws_id = uuid.uuid4()
    row_id = uuid.uuid4()
    try:
        with pg_engine.begin() as conn:
            conn.execute(
                Workspace.__table__.insert().values(id=ws_id, name="Imm", slug=f"imm-{row_id.hex}")
            )
            conn.execute(
                PolicyRuleEvaluation.__table__.insert().values(
                    id=row_id,
                    workspace_id=ws_id,
                    action="write_file",
                    base_effect="allow",
                    final_effect="deny",
                    requires_approval=False,
                    severity="critical",
                    matched_rule_ids=["r"],
                    context_redacted={"branch": "x"},
                )
            )
        with pytest.raises(DatabaseError), pg_engine.begin() as conn:
            conn.execute(
                text("UPDATE policy_rule_evaluation SET final_effect = 'allow' WHERE id = :i"),
                {"i": row_id},
            )
        with pytest.raises(DatabaseError), pg_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM policy_rule_evaluation WHERE id = :i"), {"i": row_id}
            )
    finally:
        Base.metadata.drop_all(pg_engine)
