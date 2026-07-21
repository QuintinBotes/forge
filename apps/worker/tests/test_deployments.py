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
from forge_deploy.orchestrator import DeploymentOrchestrator
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import GateConfig
from forge_worker.celery_app import celery_app
from forge_worker.tasks import deployments as deployments_task
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
        pipeline = EnvironmentPipeline(workspace_id=WS_ID, project_id=project.id, repo_id=REPO_ID)
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


# --------------------------------------------------------------- registration
def test_deployment_tasks_registered_with_celery() -> None:
    """The task module must be in the celery ``include`` list so the tasks
    register at worker boot — independent of any test-time side-effect import.

    ``loader.import_default_modules()`` is exactly what a real worker calls on
    startup to import ``conf.include``; asserting through it proves the tasks
    resolve from the include list rather than from this file's own import."""
    assert "forge_worker.tasks.deployments" in (celery_app.conf.include or [])
    celery_app.loader.import_default_modules()
    assert "forge.deployments.advance" in celery_app.tasks
    assert "forge.deployments.request" in celery_app.tasks


# ------------------------------------------------------------- task behaviour
def _seed_requested_deployment(
    factory: sessionmaker[Session], project_id: uuid.UUID
) -> uuid.UUID:
    """Persist a bare REQUESTED deployment (no advance) and return its id."""
    with factory() as s:
        repo = DeploymentRepository(s, workspace_id=WS_ID)
        pipeline = repo.get_pipeline_for_project(project_id)
        assert pipeline is not None
        env = repo.get_environment(pipeline.id, "dev")
        assert env is not None
        dep = repo.create_deployment(
            project_id=project_id,
            pipeline_id=pipeline.id,
            environment_id=env.id,
            environment_name=env.name,
            repo_id=pipeline.repo_id,
            commit_sha="abc123",
            initiated_by="system:test",
            state=DeploymentState.REQUESTED,
        )
        s.commit()
        return dep.id


def _spy_on_advance(monkeypatch) -> list[uuid.UUID]:
    calls: list[uuid.UUID] = []
    original = DeploymentOrchestrator.advance

    def _spy(self: DeploymentOrchestrator, deployment_id: uuid.UUID) -> DeploymentState:
        calls.append(deployment_id)
        return original(self, deployment_id)

    monkeypatch.setattr(DeploymentOrchestrator, "advance", _spy)
    return calls


def test_request_task_drives_orchestrator_advance(seeded, monkeypatch) -> None:
    factory, project_id = seeded
    monkeypatch.setattr(deployments_task, "_session_factory", lambda: factory)
    advanced = _spy_on_advance(monkeypatch)

    dep_id = deployments_task.request_deployment_task(
        str(WS_ID),
        str(project_id),
        DeploymentRequest(environment="dev", commit_sha="abc123").model_dump(mode="json"),
        "system:test",
    )

    assert dep_id is not None
    # The task created the deployment and drove it through the orchestrator.
    assert uuid.UUID(dep_id) in advanced
    # No CI reader wired -> ci_green gate fails closed -> terminal GATE_REJECTED.
    with factory() as s:
        state = DeploymentRepository(s, workspace_id=WS_ID).get_or_404(uuid.UUID(dep_id)).state
    assert state == DeploymentState.GATE_REJECTED


def test_advance_task_drives_orchestrator_advance(seeded, monkeypatch) -> None:
    factory, project_id = seeded
    dep_id = _seed_requested_deployment(factory, project_id)
    monkeypatch.setattr(deployments_task, "_session_factory", lambda: factory)
    advanced = _spy_on_advance(monkeypatch)

    result = deployments_task.advance_deployment(str(dep_id), str(WS_ID))

    # The task resumed the FSM via DeploymentOrchestrator.advance() on that id.
    assert dep_id in advanced
    # REQUESTED -> gate-evaluate -> no CI -> GATE_REJECTED (fail-closed).
    assert result == DeploymentState.GATE_REJECTED.value
