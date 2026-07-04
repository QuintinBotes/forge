"""Deployment gate evaluator — AC5, AC6, AC7, AC14, AC24."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from conftest import (
    WS_ID,
    FakeGitHub,
    FakePolicyReader,
    FakeValidationReader,
    make_deployment,
    seed_pipeline,
)
from sqlalchemy.orm import Session

from forge_contracts.dtos import DeployRules
from forge_deploy.freeze import FakeClock
from forge_deploy.gate import DeploymentGateEvaluator
from forge_deploy.repository import DeploymentRepository
from forge_deploy.states import (
    DeploymentState,
    DeploymentTrigger,
    GateCheckName,
    GateCheckStatus,
)

WED_NOON = FakeClock(datetime(2026, 6, 24, 12, 0, tzinfo=UTC))


def _evaluator(session: Session, **kw) -> DeploymentGateEvaluator:
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    return DeploymentGateEvaluator(
        repo,
        policy=kw.get("policy", FakePolicyReader()),
        ci=kw.get("ci", FakeGitHub()),
        validation=kw.get("validation", FakeValidationReader()),
        clock=kw.get("clock", WED_NOON),
    )


def _check(ev, name: GateCheckName) -> GateCheckStatus:
    return next(c.status for c in ev.checks if c.name == name)


def test_rank0_skips_predecessor_and_clears(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    ev = _evaluator(session).evaluate(dep.id)
    assert _check(ev, GateCheckName.PREDECESSOR_SUCCEEDED) == GateCheckStatus.SKIPPED
    assert ev.can_proceed is True
    assert ev.requires_human_approval is False  # dev unrestricted, no approval


def test_predecessor_failed_blocks(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    # staging on abc123, request production for def456 -> predecessor mismatch
    make_deployment(
        session, seeded["env"]["staging"], "abc123", state=DeploymentState.SUCCEEDED
    )
    dep = make_deployment(session, seeded["env"]["production"], "def456")
    ev = _evaluator(session).evaluate(dep.id)
    assert _check(ev, GateCheckName.PREDECESSOR_SUCCEEDED) == GateCheckStatus.FAILED
    assert ev.can_proceed is False
    assert any("def456" in r for r in ev.blocking_reasons)


def test_predecessor_passes_same_commit(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    make_deployment(
        session, seeded["env"]["staging"], "abc123", state=DeploymentState.SUCCEEDED
    )
    dep = make_deployment(session, seeded["env"]["production"], "abc123")
    ev = _evaluator(session).evaluate(dep.id)
    assert _check(ev, GateCheckName.PREDECESSOR_SUCCEEDED) == GateCheckStatus.PASSED


def test_ci_red_blocks(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    ev = _evaluator(session, ci=FakeGitHub(status="failure")).evaluate(dep.id)
    assert _check(ev, GateCheckName.CI_GREEN) == GateCheckStatus.FAILED
    assert ev.can_proceed is False


def test_ci_pending_blocks(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    ev = _evaluator(session, ci=FakeGitHub(status="pending")).evaluate(dep.id)
    assert _check(ev, GateCheckName.CI_GREEN) == GateCheckStatus.FAILED


def test_spec_validated_pass_and_skip(session: Session, project_id) -> None:
    seeded = seed_pipeline(
        session,
        project_id=project_id,
        gate_overrides={"staging": {"required_checks": ["ci_green", "spec_validated"]}},
    )
    make_deployment(
        session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED
    )
    dep = make_deployment(session, seeded["env"]["staging"], "abc123")
    # No validation -> skipped (does not block)
    ev = _evaluator(session).evaluate(dep.id)
    assert _check(ev, GateCheckName.SPEC_VALIDATED) == GateCheckStatus.SKIPPED
    # validation pass
    ev2 = _evaluator(session, validation=FakeValidationReader("pass")).evaluate(dep.id)
    assert _check(ev2, GateCheckName.SPEC_VALIDATED) == GateCheckStatus.PASSED
    # validation fail -> blocks
    ev3 = _evaluator(session, validation=FakeValidationReader("fail")).evaluate(dep.id)
    assert _check(ev3, GateCheckName.SPEC_VALIDATED) == GateCheckStatus.FAILED


def test_security_clean_skip_when_unavailable(session: Session, project_id) -> None:
    seeded = seed_pipeline(
        session,
        project_id=project_id,
        gate_overrides={"dev": {"required_checks": ["ci_green", "security_clean"]}},
    )
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    ev = _evaluator(session).evaluate(dep.id)
    assert _check(ev, GateCheckName.SECURITY_CLEAN) == GateCheckStatus.SKIPPED


@pytest.mark.parametrize("env_requires_approval", [True, False])
@pytest.mark.parametrize("trigger", [DeploymentTrigger.MANUAL, DeploymentTrigger.AGENT])
def test_restricted_always_requires_approval(
    session: Session, project_id, env_requires_approval: bool, trigger
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    staging = seeded["env"]["staging"]
    # Even if someone forces requires_approval False, restricted forces it True.
    staging.requires_approval = env_requires_approval
    session.flush()
    make_deployment(
        session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED
    )
    dep = make_deployment(session, staging, "abc123", trigger=trigger)
    ev = _evaluator(session).evaluate(dep.id)
    assert ev.requires_human_approval is True


def test_agent_without_allow_requires_approval(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    # dev is unrestricted; agent deploy with allow_agent_deploy False still gates.
    dep = make_deployment(
        session, seeded["env"]["dev"], "abc123", trigger=DeploymentTrigger.AGENT
    )
    ev = _evaluator(session).evaluate(dep.id)
    assert ev.requires_human_approval is True
    # policy still permits (human can approve), so can_proceed is not blocked by it
    assert _check(ev, GateCheckName.POLICY_ALLOWS) == GateCheckStatus.PASSED


def test_not_frozen_blocks_then_override_clears(session: Session, project_id) -> None:
    freeze = {
        "freeze_windows": [
            {"start_day": 4, "start_time": "17:00", "end_day": 0, "end_time": "09:00"}
        ]
    }
    seeded = seed_pipeline(
        session, project_id=project_id, gate_overrides={"production": freeze}
    )
    make_deployment(
        session, seeded["env"]["staging"], "abc123", state=DeploymentState.SUCCEEDED
    )
    dep = make_deployment(session, seeded["env"]["production"], "abc123")
    sat = FakeClock(datetime(2026, 6, 27, 12, 0, tzinfo=UTC))  # Saturday -> frozen
    ev = _evaluator(session, clock=sat).evaluate(dep.id)
    assert _check(ev, GateCheckName.NOT_FROZEN) == GateCheckStatus.FAILED
    assert ev.can_proceed is False
    # Override the freeze and re-evaluate.
    dep.freeze_override_by = uuid.uuid4()
    session.flush()
    ev2 = _evaluator(session, clock=sat).evaluate(dep.id)
    assert _check(ev2, GateCheckName.NOT_FROZEN) == GateCheckStatus.PASSED


def test_unknown_env_to_policy_fails_policy_check(session: Session, project_id) -> None:
    # Policy that does not list 'dev' at all.
    rules = DeployRules(allow_agent_deploy=True, environments=["prod-only"])
    seeded = seed_pipeline(session, project_id=project_id, restricted=())
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    ev = _evaluator(session, policy=FakePolicyReader(rules)).evaluate(dep.id)
    assert _check(ev, GateCheckName.POLICY_ALLOWS) == GateCheckStatus.FAILED
    assert ev.can_proceed is False


def test_persist_writes_check_rows(session: Session, project_id) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    _evaluator(session).evaluate(dep.id, persist=True)
    rows = repo.checks(dep.id)
    assert {r.name for r in rows} >= {
        GateCheckName.POLICY_ALLOWS,
        GateCheckName.PREDECESSOR_SUCCEEDED,
        GateCheckName.CI_GREEN,
        GateCheckName.NOT_FROZEN,
    }


def test_evaluate_is_total(session: Session, project_id) -> None:
    """Restricted-env cases always require approval; evaluation never raises."""
    seeded = seed_pipeline(session, project_id=project_id)
    for env_name in ("dev", "staging", "production"):
        for ci_status in ("success", "failure", None):
            for trig in (DeploymentTrigger.MANUAL, DeploymentTrigger.AGENT):
                dep = make_deployment(
                    session, seeded["env"][env_name], "abc123", trigger=trig
                )
                ev = _evaluator(session, ci=FakeGitHub(status=ci_status)).evaluate(
                    dep.id
                )
                assert ev.environment == env_name
                if seeded["env"][env_name].is_restricted:
                    assert ev.requires_human_approval is True
                # Free the environment's active slot for the next iteration.
                session.delete(dep)
                session.flush()
