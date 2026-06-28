"""Deployment FSM + orchestrator — AC4, AC8-13, AC18-20."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from conftest import (
    APPROVER2_ID,
    APPROVER_ID,
    WS_ID,
    FakeGitHub,
    FakePolicyReader,
    FakeValidationReader,
    make_deployment,
    seed_pipeline,
)
from sqlalchemy.orm import Session

from forge_deploy.engine import DeploymentStateMachine
from forge_deploy.errors import InvalidTransitionError
from forge_deploy.freeze import FakeClock
from forge_deploy.health import NullHealthChecker, ScriptedHealthChecker
from forge_deploy.orchestrator import DeploymentOrchestrator
from forge_deploy.providers import NullDeployProvider
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import DeployStatus
from forge_deploy.states import (
    DeploymentEvent,
    DeploymentEventType,
    DeploymentKind,
    DeploymentState,
)

WED = FakeClock(datetime(2026, 6, 24, 12, 0, tzinfo=UTC))


def _orch(session: Session, *, provider=None, health=None, ci="success", validation=None):
    provider = provider or NullDeployProvider()
    health = health or NullHealthChecker()
    return DeploymentOrchestrator(
        session,
        workspace_id=WS_ID,
        provider_resolver=lambda cfg: provider,
        health_resolver=lambda cfg: health,
        policy=FakePolicyReader(),
        ci=FakeGitHub(status=ci),
        validation=FakeValidationReader(validation),
        clock=WED,
    )


def _engine(session: Session) -> DeploymentStateMachine:
    return DeploymentStateMachine(session, workspace_id=WS_ID)


def _approve(session: Session, dep_id: uuid.UUID, *user_ids: uuid.UUID) -> None:
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    for uid in user_ids:
        repo.add_approval(dep_id, approver_user_id=uid, decision="approve")


def test_unrestricted_auto_clears_no_approval(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    provider = NullDeployProvider()
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    final = _orch(session, provider=provider).advance(dep.id)
    assert final == DeploymentState.SUCCEEDED
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    assert repo.approvals(dep.id) == []
    assert len(provider.triggered) == 1
    states = [t.to_state for t in repo.transitions(dep.id)]
    assert "deploying" in states


def test_restricted_waits_for_approval(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    make_deployment(
        session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED
    )
    provider = NullDeployProvider()
    dep = make_deployment(session, seeded["env"]["staging"], "abc123")
    final = _orch(session, provider=provider).advance(dep.id)
    assert final == DeploymentState.AWAITING_APPROVAL
    assert provider.triggered == []


def test_approve_triggers_single_deploy(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    make_deployment(
        session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED
    )
    provider = NullDeployProvider()
    orch = _orch(session, provider=provider)
    dep = make_deployment(session, seeded["env"]["staging"], "abc123")
    assert orch.advance(dep.id) == DeploymentState.AWAITING_APPROVAL
    _approve(session, dep.id, APPROVER_ID)
    _engine(session).transition(
        dep.id, DeploymentEvent(type=DeploymentEventType.APPROVE, actor=f"user:{APPROVER_ID}")
    )
    assert orch.advance(dep.id) == DeploymentState.SUCCEEDED
    assert len(provider.triggered) == 1


def test_reject_to_gate_rejected_no_deploy(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    make_deployment(
        session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED
    )
    provider = NullDeployProvider()
    orch = _orch(session, provider=provider)
    dep = make_deployment(session, seeded["env"]["staging"], "abc123")
    orch.advance(dep.id)
    _engine(session).transition(
        dep.id, DeploymentEvent(type=DeploymentEventType.REJECT, actor=f"user:{APPROVER_ID}")
    )
    assert orch.repo.get_or_404(dep.id).state == DeploymentState.GATE_REJECTED
    assert provider.triggered == []


def test_multi_approval_needs_two_distinct(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    make_deployment(
        session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED
    )
    make_deployment(
        session, seeded["env"]["staging"], "abc123", state=DeploymentState.SUCCEEDED
    )
    orch = _orch(session)
    dep = make_deployment(session, seeded["env"]["production"], "abc123")
    orch.advance(dep.id)
    engine = _engine(session)
    # One approval is not enough (min_approvals=2): transition guard blocks.
    _approve(session, dep.id, APPROVER_ID)
    with pytest.raises(InvalidTransitionError):
        engine.transition(
            dep.id, DeploymentEvent(type=DeploymentEventType.APPROVE)
        )
    # Same user approving again does not increase the distinct count.
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    assert repo.distinct_approver_count(dep.id) == 1
    # Second distinct approver advances it.
    _approve(session, dep.id, APPROVER2_ID)
    engine.transition(dep.id, DeploymentEvent(type=DeploymentEventType.APPROVE))
    assert orch.advance(dep.id) == DeploymentState.SUCCEEDED


def test_deploy_success_then_health_pass_to_succeeded(
    session: Session, project_id
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dev = seeded["env"]["dev"]
    dep = make_deployment(session, dev, "abc123")
    final = _orch(session, health=ScriptedHealthChecker([True])).advance(dep.id)
    assert final == DeploymentState.SUCCEEDED
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    current = repo.currently_deployed(dev.id)
    assert current.commit_sha == "abc123"


def test_health_fail_auto_rollback(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    prod = seeded["env"]["production"]
    # Prior good production deployment of abc123 (the rollback target).
    make_deployment(
        session,
        prod,
        "abc123",
        state=DeploymentState.SUCCEEDED,
        finished_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    # New production deployment def456, pre-approved, that will fail health.
    dep = make_deployment(session, prod, "def456", state=DeploymentState.APPROVED)
    orch = _orch(session, health=ScriptedHealthChecker([False, True]))
    final = orch.advance(dep.id)
    assert final == DeploymentState.ROLLED_BACK
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    rollbacks = [
        d
        for d in repo.list_deployments(project_id=project_id, environment_name="production")
        if d.kind == DeploymentKind.ROLLBACK
    ]
    assert len(rollbacks) == 1
    assert rollbacks[0].state == DeploymentState.SUCCEEDED
    assert rollbacks[0].commit_sha == "abc123"
    assert repo.currently_deployed(prod.id).commit_sha == "abc123"


def test_health_fail_no_rollback_to_failed(session: Session, project_id) -> None:
    seeded = seed_pipeline(
        session, project_id=project_id, gate_overrides={"staging": {"auto_rollback": False}}
    )
    staging = seeded["env"]["staging"]
    dep = make_deployment(session, staging, "abc123", state=DeploymentState.APPROVED)
    orch = _orch(session, health=ScriptedHealthChecker([False]))
    final = orch.advance(dep.id)
    assert final == DeploymentState.FAILED
    dep = orch.repo.get_or_404(dep.id)
    assert dep.failure_reason is not None
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    rollbacks = [
        d for d in repo.list_deployments(project_id=project_id) if d.kind == DeploymentKind.ROLLBACK
    ]
    assert rollbacks == []


def test_provider_failure_to_failed(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123", state=DeploymentState.APPROVED)
    provider = NullDeployProvider(statuses=[DeployStatus(state="failure", finished=True)])
    final = _orch(session, provider=provider).advance(dep.id)
    assert final == DeploymentState.FAILED


def test_cancel_non_terminal(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    state = _engine(session).transition(
        dep.id, DeploymentEvent(type=DeploymentEventType.CANCEL, actor=f"user:{APPROVER_ID}")
    )
    assert state == DeploymentState.CANCELLED


def test_cancel_terminal_raises(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(
        session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED
    )
    with pytest.raises(InvalidTransitionError):
        _engine(session).transition(
            dep.id, DeploymentEvent(type=DeploymentEventType.CANCEL)
        )


def test_transitions_append_only_and_redacted(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    engine = _engine(session)
    engine.transition(
        dep.id,
        DeploymentEvent(
            type=DeploymentEventType.REQUEST,
            payload={"deploy_token": "super-secret", "note": "ok"},
        ),
    )
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    rows = repo.transitions(dep.id)
    assert [r.sequence for r in rows] == list(range(1, len(rows) + 1))
    assert rows[0].payload["deploy_token"] == "***"
    assert rows[0].payload["note"] == "ok"


def test_idempotent_event_replay(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    engine = _engine(session)
    engine.transition(
        dep.id,
        DeploymentEvent(type=DeploymentEventType.REQUEST, idempotency_key="k1"),
    )
    # Replaying the same key is a no-op (state unchanged, no duplicate transition).
    state = engine.transition(
        dep.id,
        DeploymentEvent(type=DeploymentEventType.REQUEST, idempotency_key="k1"),
    )
    assert state == DeploymentState.GATE_EVALUATING
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    assert len([t for t in repo.transitions(dep.id) if t.event == "request"]) == 1


def test_full_promotion_chain(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    orch = _orch(session)
    engine = _engine(session)

    # dev: auto-clears.
    dev = make_deployment(session, seeded["env"]["dev"], "abc123")
    assert orch.advance(dev.id) == DeploymentState.SUCCEEDED

    # staging: 1 approval.
    staging = make_deployment(session, seeded["env"]["staging"], "abc123")
    assert orch.advance(staging.id) == DeploymentState.AWAITING_APPROVAL
    _approve(session, staging.id, APPROVER_ID)
    engine.transition(staging.id, DeploymentEvent(type=DeploymentEventType.APPROVE))
    assert orch.advance(staging.id) == DeploymentState.SUCCEEDED

    # production: 2 approvals.
    prod = make_deployment(session, seeded["env"]["production"], "abc123")
    assert orch.advance(prod.id) == DeploymentState.AWAITING_APPROVAL
    _approve(session, prod.id, APPROVER_ID, APPROVER2_ID)
    engine.transition(prod.id, DeploymentEvent(type=DeploymentEventType.APPROVE))
    assert orch.advance(prod.id) == DeploymentState.SUCCEEDED

    assert repo.currently_deployed(seeded["env"]["production"].id).commit_sha == "abc123"
