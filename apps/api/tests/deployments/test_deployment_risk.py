"""The API's deploy gate reads the change's risk (#194 criterion 9)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from conftest import WS_ID, configure_pipeline
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from scf_contracts import GateRequirements, RiskClass, UserRole
from scf_db.models import RiskClassification, RiskGateObservation


def _classify(factory: sessionmaker[Session], commit_sha: str, mode: str) -> None:
    gated = GateRequirements(
        risk_class=RiskClass.HIGH, approvals=2, deploy_mode="gated", red_team=False, self_eval=True
    )
    with factory() as s:
        s.add(
            RiskClassification(
                workspace_id=WS_ID,
                commit_sha=commit_sha,
                derived_class=RiskClass.HIGH,
                final_class=RiskClass.HIGH,
                enforcement_mode=mode,
                class_requirements={"high": gated.model_dump(mode="json")},
            )
        )
        s.commit()


def test_an_enforced_class_gates_a_dev_deploy_and_the_preview_writes_nothing(
    client_factory: Callable[..., TestClient],
    project_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    _classify(session_factory, "abc123", "enforcing")
    configure_pipeline(client_factory(UserRole.ADMIN), project_id)
    member = client_factory(UserRole.MEMBER)

    resp = member.post(
        f"/projects/{project_id}/deployments", json={"environment": "dev", "commit_sha": "abc123"}
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["state"] == "awaiting_approval"
    gate = member.get(f"/deployments/{resp.json()['id']}/gate")
    assert gate.status_code == 200 and gate.json()["requires_human_approval"] is True
    with session_factory() as s:
        rows = list(s.scalars(select(RiskGateObservation)))
    assert [(r.gate_name, r.actual_decision, r.risk_decision) for r in rows] == [
        ("deploy", "auto", "gated")
    ]


def test_a_shadowed_class_leaves_a_dev_deploy_as_it_was(
    client_factory: Callable[..., TestClient],
    project_id: uuid.UUID,
    session_factory: sessionmaker[Session],
) -> None:
    _classify(session_factory, "abc123", "shadow")
    configure_pipeline(client_factory(UserRole.ADMIN), project_id)
    resp = client_factory(UserRole.MEMBER).post(
        f"/projects/{project_id}/deployments", json={"environment": "dev", "commit_sha": "abc123"}
    )
    assert resp.json()["state"] == "succeeded"
