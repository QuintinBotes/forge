"""Pipeline resolution + policy binding.

``resolve_environments`` is the pure validation the pipeline upsert relies on:
every environment must be declared in the repo ``deploy_rules`` and
``is_restricted`` is **derived from policy** (never user-relaxable). The
:class:`PipelineResolver` exposes the read helpers (ordering, predecessor,
currently-deployed) the gate and API surface use.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from forge_contracts.dtos import DeployRules
from forge_db.models.deployment import Deployment, Environment, EnvironmentPipeline
from forge_deploy.errors import RuleValidationError
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import EnvironmentSpec, PipelineSpec


@dataclass
class ResolvedEnvironment:
    name: str
    rank: int
    is_restricted: bool
    requires_approval: bool
    gate_config: dict[str, Any] = field(default_factory=dict)
    provider_config: dict[str, Any] = field(default_factory=dict)
    health_check: dict[str, Any] = field(default_factory=dict)


def _norm(value: str) -> str:
    return value.strip().lower()


def resolve_environments(
    spec: PipelineSpec,
    rules: DeployRules,
    *,
    requested_restricted: dict[str, bool] | None = None,
) -> list[ResolvedEnvironment]:
    """Validate a pipeline spec against policy and derive ``is_restricted``.

    Raises :class:`RuleValidationError` if an environment is unknown to policy, or
    if the caller tries to relax a policy-restricted environment to unrestricted.
    """
    allowed = {_norm(e) for e in rules.environments}
    restricted = {_norm(e) for e in rules.restricted_environments}
    known = allowed | restricted
    requested_restricted = requested_restricted or {}

    resolved: list[ResolvedEnvironment] = []
    for env in sorted(spec.environments, key=lambda e: e.rank):
        if known and _norm(env.name) not in known:
            raise RuleValidationError(
                f"environment {env.name!r} is not declared in deploy_rules "
                f"(environments or restricted_environments)"
            )
        is_restricted = _norm(env.name) in restricted
        if (
            is_restricted
            and requested_restricted.get(env.name) is False
        ):
            raise RuleValidationError(
                f"environment {env.name!r} is policy-restricted and cannot be "
                f"set unrestricted"
            )
        resolved.append(_to_resolved(env, is_restricted))
    return resolved


def _to_resolved(env: EnvironmentSpec, is_restricted: bool) -> ResolvedEnvironment:
    return ResolvedEnvironment(
        name=env.name,
        rank=env.rank,
        is_restricted=is_restricted,
        # Restricted environments always require approval.
        requires_approval=True if is_restricted else env.requires_approval,
        gate_config=env.gate_config.model_dump(mode="json"),
        provider_config=env.provider_config.model_dump(mode="json"),
        health_check=env.health_check.model_dump(mode="json"),
    )


class PipelineResolver:
    def __init__(self, repo: DeploymentRepository) -> None:
        self.repo = repo

    def get(self, project_id: uuid.UUID) -> EnvironmentPipeline | None:
        return self.repo.get_pipeline_for_project(project_id)

    def ordered(self, pipeline_id: uuid.UUID) -> list[Environment]:
        return self.repo.environments(pipeline_id)

    def predecessor(self, environment: Environment) -> Environment | None:
        return self.repo.predecessor(environment)

    def currently_deployed(self, environment_id: uuid.UUID) -> Deployment | None:
        return self.repo.currently_deployed(environment_id)


__all__ = [
    "PipelineResolver",
    "ResolvedEnvironment",
    "resolve_environments",
]
