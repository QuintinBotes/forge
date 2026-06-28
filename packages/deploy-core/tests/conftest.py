"""Shared fixtures for the forge_deploy engine unit suite.

Hermetic: SQLite in-memory (JSON variant) + NullDeployProvider + scripted
HealthChecker + fake GitHub/policy/validation readers + FakeClock. No network,
no Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.dtos import DeployRules
from forge_db.base import Base
from forge_db.models import Project, User, Workspace
from forge_db.models.deployment import (
    Deployment,
    Environment,
    EnvironmentPipeline,
)
from forge_deploy.freeze import FakeClock
from forge_deploy.schemas import (
    DeployProviderConfig,
    EnvironmentSpec,
    GateConfig,
    HealthCheckSpec,
    PipelineSpec,
)
from forge_deploy.states import DeploymentKind, DeploymentState, DeploymentTrigger

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
APPROVER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
APPROVER2_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b4")
REPO_ID = "github.com/org/api"


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        s.add(Workspace(id=OTHER_WS_ID, name="Other", slug="other"))
        s.flush()
        for uid, email in (
            (USER_ID, "dev@acme.test"),
            (APPROVER_ID, "approver@acme.test"),
            (APPROVER2_ID, "approver2@acme.test"),
        ):
            s.add(User(id=uid, workspace_id=WS_ID, email=email, name="U", role="member"))
        project = Project(workspace_id=WS_ID, name="API", key="API")
        s.add(project)
        s.flush()
        _PROJECT["id"] = project.id
        s.commit()
    return factory


_PROJECT: dict[str, uuid.UUID] = {}


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def project_id(session: Session) -> uuid.UUID:
    return _PROJECT["id"]


@pytest.fixture
def pipeline_spec() -> PipelineSpec:
    return PipelineSpec(
        repo_id=REPO_ID,
        environments=[
            EnvironmentSpec(
                name="dev",
                rank=0,
                requires_approval=False,
                gate_config=GateConfig(required_checks=["ci_green"]),
                provider_config=DeployProviderConfig(provider="null"),
                health_check=HealthCheckSpec(kind="none"),
            ),
            EnvironmentSpec(
                name="staging",
                rank=1,
                gate_config=GateConfig(required_checks=["ci_green"], min_approvals=1),
                provider_config=DeployProviderConfig(provider="null"),
                health_check=HealthCheckSpec(kind="none"),
            ),
            EnvironmentSpec(
                name="production",
                rank=2,
                gate_config=GateConfig(
                    required_checks=["ci_green"], min_approvals=2, auto_rollback=True
                ),
                provider_config=DeployProviderConfig(provider="null"),
                health_check=HealthCheckSpec(kind="none"),
            ),
        ],
    )


def seed_pipeline(
    session: Session,
    *,
    project_id: uuid.UUID,
    restricted: tuple[str, ...] = ("staging", "production"),
    gate_overrides: dict[str, dict[str, Any]] | None = None,
    workspace_id: uuid.UUID = WS_ID,
) -> dict[str, Any]:
    """Create a pipeline + 3 environments. Returns {'pipeline', 'env': {name: row}}."""
    gate_overrides = gate_overrides or {}
    pipeline = EnvironmentPipeline(
        workspace_id=workspace_id, project_id=project_id, repo_id=REPO_ID
    )
    session.add(pipeline)
    session.flush()
    envs: dict[str, Environment] = {}
    defaults = {
        "dev": {
            "rank": 0,
            "requires_approval": False,
            "gate": {"required_checks": ["ci_green"]},
        },
        "staging": {
            "rank": 1,
            "requires_approval": True,
            "gate": {"required_checks": ["ci_green"], "min_approvals": 1},
        },
        "production": {
            "rank": 2,
            "requires_approval": True,
            "gate": {
                "required_checks": ["ci_green"],
                "min_approvals": 2,
                "auto_rollback": True,
            },
        },
    }
    for name, cfg in defaults.items():
        gate = dict(cfg["gate"])
        gate.update(gate_overrides.get(name, {}))
        is_restricted = name in restricted
        env = Environment(
            workspace_id=workspace_id,
            pipeline_id=pipeline.id,
            name=name,
            rank=cfg["rank"],
            is_restricted=is_restricted,
            requires_approval=True if is_restricted else cfg["requires_approval"],
            gate_config=GateConfig.model_validate(gate).model_dump(mode="json"),
            provider_config={"provider": "null"},
            health_check={"kind": "none"},
        )
        session.add(env)
        envs[name] = env
    session.flush()
    return {"pipeline": pipeline, "env": envs}


def make_deployment(
    session: Session,
    env: Environment,
    commit: str,
    *,
    state: DeploymentState = DeploymentState.REQUESTED,
    kind: DeploymentKind = DeploymentKind.PROMOTION,
    trigger: DeploymentTrigger = DeploymentTrigger.MANUAL,
    initiated_by: str = f"user:{USER_ID}",
    from_environment_name: str | None = None,
    workspace_id: uuid.UUID = WS_ID,
    idempotency_key: str | None = None,
    finished_at: datetime | None = None,
) -> Deployment:
    dep = Deployment(
        workspace_id=workspace_id,
        project_id=env.pipeline.project_id if env.pipeline else _PROJECT["id"],
        pipeline_id=env.pipeline_id,
        environment_id=env.id,
        environment_name=env.name,
        repo_id=REPO_ID,
        commit_sha=commit,
        from_environment_name=from_environment_name,
        kind=kind,
        state=state,
        trigger=trigger,
        initiated_by=initiated_by,
        idempotency_key=idempotency_key,
        requested_at=datetime.now(UTC),
        finished_at=finished_at,
    )
    session.add(dep)
    session.flush()
    return dep


class FakeGitHub:
    def __init__(self, status: str | None = "success") -> None:
        self.status = status
        self.created: list[dict[str, Any]] = []

    def get_combined_status(self, repo_id: str, commit_sha: str) -> str | None:
        return self.status


class FakePolicyReader:
    def __init__(self, rules: DeployRules | None = None) -> None:
        self.rules = rules or DeployRules(
            allow_agent_deploy=False,
            environments=["dev"],
            restricted_environments=["staging", "production"],
        )

    def deploy_rules(self, repo_id: str) -> DeployRules:
        return self.rules


class FakeValidationReader:
    def __init__(self, status: str | None = None) -> None:
        self.status = status

    def validation_status(self, repo_id: str, commit_sha: str) -> str | None:
        return self.status


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def policy_reader() -> FakePolicyReader:
    return FakePolicyReader()


@pytest.fixture
def validation_reader() -> FakeValidationReader:
    return FakeValidationReader()


@pytest.fixture
def freeze_clock() -> FakeClock:
    # A Wednesday noon UTC — outside the canonical Fri->Mon freeze window.
    return FakeClock(datetime(2026, 6, 24, 12, 0, tzinfo=UTC))
