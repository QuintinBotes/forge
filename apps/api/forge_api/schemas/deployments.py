"""Request/response models for the deployments router (F31)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts.deployment import DeploymentDTO
from forge_deploy.schemas import (
    DeployProviderConfig,
    GateCheckResult,
    GateConfig,
    GateEvaluation,
    HealthCheckSpec,
)


class EnvironmentUpsert(BaseModel):
    """A pipeline stage in an upsert body.

    ``is_restricted`` is optional and *advisory*: it is derived from policy. If a
    caller sends ``is_restricted=false`` for a policy-restricted env, the upsert
    returns 422 (it cannot be relaxed).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    rank: int = Field(ge=0)
    requires_approval: bool = True
    is_restricted: bool | None = None
    gate_config: GateConfig = Field(default_factory=GateConfig)
    provider_config: DeployProviderConfig = Field(default_factory=DeployProviderConfig)
    health_check: HealthCheckSpec = Field(default_factory=HealthCheckSpec)


class PipelineUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    enabled: bool = True
    version: int = 1
    environments: list[EnvironmentUpsert] = Field(min_length=1)


class EnvironmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    rank: int
    is_restricted: bool
    requires_approval: bool
    gate_config: dict[str, Any]
    provider_config: dict[str, Any]
    health_check: dict[str, Any]
    currently_deployed: DeploymentDTO | None = None


class PipelineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    repo_id: str
    enabled: bool
    version: int
    environments: list[EnvironmentRead]


class DeploymentRead(DeploymentDTO):
    pass


class DeploymentTransitionRead(BaseModel):
    sequence: int
    from_state: str
    to_state: str
    event: str
    actor: str
    created_at: Any | None = None


class DeploymentDetail(DeploymentRead):
    gate: GateEvaluation | None = None
    checks: list[GateCheckResult] = Field(default_factory=list)
    transitions: list[DeploymentTransitionRead] = Field(default_factory=list)
    diff_since: dict[str, Any] | None = None


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str  # "approve" | "reject" | "changes_requested"
    note: str | None = None


class FreezeOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


__all__ = [
    "DecisionRequest",
    "DeploymentDetail",
    "DeploymentRead",
    "DeploymentTransitionRead",
    "EnvironmentRead",
    "EnvironmentUpsert",
    "FreezeOverrideRequest",
    "PipelineRead",
    "PipelineUpsert",
]
