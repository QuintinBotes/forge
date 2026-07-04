"""Pipeline resolver + policy binding (AC1, AC2, AC15)."""

from __future__ import annotations

import uuid

import pytest
from conftest import WS_ID, make_deployment, seed_pipeline
from sqlalchemy.orm import Session

from forge_contracts.dtos import DeployRules
from forge_deploy.errors import RuleValidationError
from forge_deploy.pipeline import PipelineResolver, resolve_environments
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import (
    DeployProviderConfig,
    EnvironmentSpec,
    PipelineSpec,
)
from forge_deploy.states import DeploymentState

RULES = DeployRules(
    allow_agent_deploy=False,
    environments=["dev"],
    restricted_environments=["staging", "production"],
)


def test_restricted_derived_from_policy(pipeline_spec: PipelineSpec) -> None:
    resolved = resolve_environments(pipeline_spec, RULES)
    by_name = {e.name: e for e in resolved}
    assert by_name["dev"].is_restricted is False
    assert by_name["staging"].is_restricted is True
    assert by_name["production"].is_restricted is True
    # Restricted envs always require approval regardless of spec.
    assert by_name["production"].requires_approval is True


def test_restricted_unset_rejected(pipeline_spec: PipelineSpec) -> None:
    with pytest.raises(RuleValidationError):
        resolve_environments(
            pipeline_spec, RULES, requested_restricted={"production": False}
        )


def test_unknown_env_rejected() -> None:
    spec = PipelineSpec(
        repo_id="github.com/org/api",
        environments=[
            EnvironmentSpec(
                name="qa", rank=0, provider_config=DeployProviderConfig(provider="null")
            )
        ],
    )
    with pytest.raises(RuleValidationError):
        resolve_environments(spec, RULES)


def test_ordering_by_rank(pipeline_spec: PipelineSpec) -> None:
    resolved = resolve_environments(pipeline_spec, RULES)
    assert [e.name for e in resolved] == ["dev", "staging", "production"]


def test_predecessor_lookup(session: Session, project_id: uuid.UUID) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    repo = DeploymentRepository(session, workspace_id=WS_ID)
    resolver = PipelineResolver(repo)
    envs = seeded["env"]
    assert resolver.predecessor(envs["dev"]) is None
    assert resolver.predecessor(envs["staging"]).name == "dev"
    assert resolver.predecessor(envs["production"]).name == "staging"


def test_currently_deployed_returns_last_succeeded(
    session: Session, project_id: uuid.UUID
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dev = seeded["env"]["dev"]
    repo = DeploymentRepository(session, workspace_id=WS_ID)

    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    make_deployment(
        session, dev, "old111", state=DeploymentState.SUCCEEDED, finished_at=base
    )
    make_deployment(
        session,
        dev,
        "new222",
        state=DeploymentState.SUCCEEDED,
        finished_at=base + timedelta(hours=1),
    )
    make_deployment(
        session,
        dev,
        "fail333",
        state=DeploymentState.FAILED,
        finished_at=base + timedelta(hours=2),
    )

    current = repo.currently_deployed(dev.id)
    assert current is not None
    assert current.commit_sha == "new222"


def test_predecessor_succeeded_same_commit_only(
    session: Session, project_id: uuid.UUID
) -> None:
    seeded = seed_pipeline(session, project_id=project_id)
    dev, staging = seeded["env"]["dev"], seeded["env"]["staging"]
    repo = DeploymentRepository(session, workspace_id=WS_ID)

    make_deployment(session, dev, "abc123", state=DeploymentState.SUCCEEDED)
    assert repo.predecessor_succeeded_for(staging, "abc123") is not None
    assert repo.predecessor_succeeded_for(staging, "def456") is None
