"""Risk at the deploy gate (#194 criteria 3, 6 and 8).

Every case of ``test_gate`` is collected here again and re-run with a shadow
reader that classifies every change as strictly as possible: each must still pass
unchanged, with one observation per evaluation. Enforcing mode forces a gated
deploy when the class demands one and never relaxes a gate that already is.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import test_gate as _matrix
from conftest import WS_ID, FakeGitHub, FakePolicyReader, make_deployment, seed_pipeline
from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_contracts import GatedChange, GateRequirements, RiskClass
from scf_contracts.testing import RecordingRiskReader
from scf_db.models import RiskClassification, RiskGateObservation
from scf_db.risk_repository import DbRiskReader
from scf_deploy.gate import DeploymentGateEvaluator
from scf_deploy.repository import DeploymentRepository
from scf_deploy.states import DeploymentState

# Criterion 3: the existing gate matrix, collected again under the shadow reader below.
globals().update({name: test for name, test in vars(_matrix).items() if name.startswith("test_")})


@pytest.fixture(autouse=True)
def _shadow(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.function.__module__ != _matrix.__name__:
        yield
        return
    reader = RecordingRiskReader("shadow")
    init = DeploymentGateEvaluator.__init__

    def with_risk(self: DeploymentGateEvaluator, *args: Any, **kwargs: Any) -> None:
        init(self, *args, risk=reader, **kwargs)

    monkeypatch.setattr(DeploymentGateEvaluator, "__init__", with_risk)
    yield
    assert reader.looked_up, "every case evaluates the gate"
    reader.assert_observed_each_lookup("deploy", {"auto", "gated"})


def _evaluate(session: Session, dep_id: uuid.UUID, risk: Any) -> Any:
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    gate = DeploymentGateEvaluator(repo, policy=FakePolicyReader(), ci=FakeGitHub(), risk=risk)
    return gate.evaluate(dep_id)


def test_shadow_keeps_an_auto_deploy_and_records_that_risk_would_gate_it(
    session: Session, project_id: uuid.UUID
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    reader = RecordingRiskReader("shadow")

    ev = _evaluate(session, dep.id, reader)

    assert (ev.can_proceed, ev.requires_human_approval) == (True, False)
    assert reader.looked_up == [GatedChange(workspace_id=WS_ID, commit_sha="abc123")]
    assert reader.observed == [{"gate": "deploy", "actual": "auto", "risk_decision": "gated"}]


def test_enforcing_gates_a_deploy_the_class_demands_gated(
    session: Session, project_id: uuid.UUID
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")

    ev = _evaluate(session, dep.id, RecordingRiskReader("enforcing"))

    assert ev.requires_human_approval is True
    assert ev.can_proceed is True  # risk adds an approval, it does not fail a check


def test_enforcing_never_ungates_a_deploy_that_is_already_gated(
    session: Session, project_id: uuid.UUID
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    make_deployment(session, seeded["env"]["dev"], "abc123", state=DeploymentState.SUCCEEDED)
    dep = make_deployment(session, seeded["env"]["staging"], "abc123")  # restricted
    reader = RecordingRiskReader("enforcing", deploy_mode="auto")

    ev = _evaluate(session, dep.id, reader)

    assert ev.requires_human_approval is True
    assert reader.observed == [{"gate": "deploy", "actual": "gated", "risk_decision": "gated"}]


def test_switching_one_repository_to_enforcing_leaves_another_unchanged(
    session: Session, project_id: uuid.UUID
) -> None:
    """Criterion 8: the mode is the one each repository's policy recorded with its change."""
    gated = GateRequirements(
        risk_class=RiskClass.HIGH, approvals=2, deploy_mode="gated", red_team=False, self_eval=True
    ).model_dump(mode="json")
    for commit, mode in (("enforced1", "enforcing"), ("shadowed2", "shadow")):
        session.add(
            RiskClassification(
                workspace_id=WS_ID,
                commit_sha=commit,
                derived_class=RiskClass.HIGH,
                final_class=RiskClass.HIGH,
                enforcement_mode=mode,
                class_requirements={"high": gated},
            )
        )
    session.commit()
    seeded = seed_pipeline(session, project_id=project_id)
    reader = DbRiskReader(session)

    # One active deployment per environment: the first is done before the second is asked.
    dev = seeded["env"]["dev"]
    enforced = make_deployment(session, dev, "enforced1", state=DeploymentState.SUCCEEDED)
    assert _evaluate(session, enforced.id, reader).requires_human_approval is True
    shadowed = make_deployment(session, dev, "shadowed2")
    assert _evaluate(session, shadowed.id, reader).requires_human_approval is False

    rows = session.scalars(select(RiskGateObservation)).all()
    assert sorted((r.gate_name, r.actual_decision, r.risk_decision, r.agreed) for r in rows) == [
        ("deploy", "auto", "gated", False),
        ("deploy", "auto", "gated", False),
    ]


def test_a_deploy_without_a_classification_is_decided_as_before(
    session: Session, project_id: uuid.UUID
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dep = make_deployment(session, seeded["env"]["dev"], "abc123")
    ev = _evaluate(session, dep.id, DbRiskReader(session))
    assert (ev.can_proceed, ev.requires_human_approval) == (True, False)
