"""The API's deploy gate names a deploy's commit through the repository host (#335).

The service binds the host to the caller's workspace; a branch name reads the
classified change's risk, and a name the host cannot resolve is refused, with
the reason, when the repository enforces risk rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from conftest import REPO_ID, WS_ID, FakeGitHub, FakePolicy, configure_pipeline
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.observability.audit import AuditLog
from scf_api.services.deployment_service import DeploymentService
from scf_contracts import GateRequirements, RiskClass, UserRole
from scf_contracts.dtos import DeployRules
from scf_db.models import AgentRun, RiskClassification, RiskGateObservation, RunStatus
from scf_deploy.freeze import FakeClock

HEAD = "3d" * 20
GATED = GateRequirements(
    risk_class=RiskClass.HIGH, approvals=2, deploy_mode="gated", red_team=False, self_eval=True
).model_dump(mode="json")


class Host:
    """The repository host as every workspace reaches it: ``main`` is the classified head."""

    def __init__(self) -> None:
        self.asked: list[tuple[uuid.UUID, str, str]] = []

    def __call__(self, workspace_id: uuid.UUID, repo_id: str, ref: str) -> str | None:
        self.asked.append((workspace_id, repo_id, ref))
        return HEAD if ref == "main" else None


@pytest.fixture
def host() -> Host:
    return Host()


@pytest.fixture
def service(
    session_factory: sessionmaker[Session], clock: FakeClock, policy_rules: DeployRules, host: Host
) -> DeploymentService:
    return DeploymentService(
        session_factory=session_factory,
        audit=AuditLog(),
        policy_reader=FakePolicy(policy_rules),
        ci_reader=FakeGitHub("success"),
        clock=clock,
        commit_resolver=host,
    )


@pytest.fixture(autouse=True)
def _enforcing_change(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        run = AgentRun(
            workspace_id=WS_ID,
            status=RunStatus.SUCCEEDED,
            inputs={"context": {"repo_full_name": "org/api"}},
            created_at=datetime(2026, 11, 20, tzinfo=UTC),
        )
        s.add(run)
        s.flush()
        s.add(
            RiskClassification(
                workspace_id=WS_ID,
                commit_sha=HEAD,
                agent_run_id=run.id,
                derived_class=RiskClass.HIGH,
                final_class=RiskClass.HIGH,
                enforcement_mode="enforcing",
                class_requirements={"high": GATED},
            )
        )
        s.commit()


def _deploy(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID, commit: str
) -> dict[str, object]:
    configure_pipeline(client_factory(UserRole.ADMIN), project_id)
    resp = client_factory(UserRole.MEMBER).post(
        f"/projects/{project_id}/deployments", json={"environment": "dev", "commit_sha": commit}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_branch_name_reads_the_risk_of_the_change_it_points_at(
    client_factory: Callable[..., TestClient],
    project_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    host: Host,
) -> None:
    deployment = _deploy(client_factory, project_id, "main")

    assert deployment["state"] == "awaiting_approval"
    assert deployment["commit_sha"] == "main"  # the deploy keeps the name it was given
    assert host.asked == [(WS_ID, REPO_ID, "main")]
    with session_factory() as s:
        rows = list(s.scalars(select(RiskGateObservation)))
    assert [(r.gate_name, r.actual_decision, r.risk_decision) for r in rows] == [
        ("deploy", "auto", "gated")
    ]


def test_an_enforcing_repository_refuses_a_name_the_host_cannot_resolve(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    deployment = _deploy(client_factory, project_id, "release-candidate")

    assert deployment["state"] == "gate_rejected"
    assert "'release-candidate' could not be resolved to a full hash" in str(
        deployment["failure_reason"]
    )
