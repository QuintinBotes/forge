"""Worker deployment driver — request_deployment_sync + CeleryDeploymentRequester."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.deployment import DeploymentRequest, DeploymentState
from forge_contracts.dtos import DeployRules
from forge_db.base import Base
from forge_db.models import Project, User, Workspace
from forge_db.models.deployment import Environment, EnvironmentPipeline
from forge_deploy.schemas import GateConfig
from forge_worker.tasks.deployments import (
    CeleryDeploymentRequester,
    request_deployment_sync,
)

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
REPO_ID = "github.com/org/api"


class _Policy:
    def deploy_rules(self, repo_id: str) -> DeployRules:
        return DeployRules(environments=["dev"], restricted_environments=["staging"])


class _CI:
    def get_combined_status(self, repo_id: str, commit_sha: str) -> str:
        return "success"


@pytest.fixture
def seeded() -> tuple[sessionmaker[Session], uuid.UUID]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        s.flush()
        s.add(User(id=USER_ID, workspace_id=WS_ID, email="d@a.test", name="D", role="member"))
        project = Project(workspace_id=WS_ID, name="API", key="API")
        s.add(project)
        s.flush()
        pipeline = EnvironmentPipeline(
            workspace_id=WS_ID, project_id=project.id, repo_id=REPO_ID
        )
        s.add(pipeline)
        s.flush()
        s.add(
            Environment(
                workspace_id=WS_ID,
                pipeline_id=pipeline.id,
                name="dev",
                rank=0,
                is_restricted=False,
                requires_approval=False,
                gate_config=GateConfig(required_checks=["ci_green"]).model_dump(mode="json"),
                provider_config={"provider": "null"},
                health_check={"kind": "none"},
            )
        )
        s.commit()
        pid = project.id
    return factory, pid


def test_request_deployment_sync_drives_to_succeeded(seeded) -> None:
    factory, project_id = seeded
    dto = request_deployment_sync(
        factory,
        workspace_id=WS_ID,
        project_id=project_id,
        request=DeploymentRequest(environment="dev", commit_sha="abc123"),
        initiated_by="system:auto_promote",
        policy=_Policy(),
        ci=_CI(),
    )
    assert dto is not None
    assert dto.state == DeploymentState.SUCCEEDED


def test_requester_protocol(seeded) -> None:
    factory, project_id = seeded
    requester = CeleryDeploymentRequester(factory, workspace_id=WS_ID)
    dto = requester.request_promotion(
        project_id=project_id,
        request=DeploymentRequest(environment="dev", commit_sha="abc123"),
        initiated_by="system:auto_promote",
    )
    # No policy/ci wired -> ci_green cannot confirm -> gate_rejected (fail-closed).
    assert dto is not None
    assert dto.state == DeploymentState.GATE_REJECTED


def test_unknown_pipeline_returns_none(seeded) -> None:
    factory, _ = seeded
    dto = request_deployment_sync(
        factory,
        workspace_id=WS_ID,
        project_id=uuid.uuid4(),
        request=DeploymentRequest(environment="dev", commit_sha="abc123"),
        initiated_by="x",
    )
    assert dto is None
