"""Pydantic schemas for pipeline config, gate evaluation, providers, and health.

These are YAML/JSON-portable (pipeline editor + ``examples/deployments/*.yaml``).
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge_deploy.states import (
    GateCheckName,
    GateCheckStatus,
    HealthStatus,
)


# --------------------------------------------------------------------------- #
# Pipeline / environment config                                               #
# --------------------------------------------------------------------------- #
class FreezeWindow(BaseModel):
    """A weekly recurring window during which deploys are blocked.

    ``start_day``/``end_day`` are 0=Mon..6=Sun in the pipeline timezone. A window
    may wrap across the week boundary (e.g. Fri 17:00 -> Mon 09:00).
    """

    model_config = ConfigDict(extra="forbid")

    start_day: int = Field(ge=0, le=6)
    start_time: time
    end_day: int = Field(ge=0, le=6)
    end_time: time
    reason: str = "release freeze"


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_checks: list[GateCheckName] = Field(default_factory=lambda: [GateCheckName.CI_GREEN])
    approver_user_ids: list[uuid.UUID] = Field(default_factory=list)
    approver_team_ids: list[uuid.UUID] = Field(default_factory=list)
    min_approvals: int = 1
    freeze_windows: list[FreezeWindow] = Field(default_factory=list)
    timezone: str = "UTC"
    auto_rollback: bool = False
    auto_promote_on_merge: bool = False
    rollback_requires_approval: bool = False
    deploy_timeout_s: int = 1800


class DeployProviderConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str = "null"


class HealthCheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["http", "command", "none"] = "none"
    url: str | None = None
    expect_status: int = 200
    command: str | None = None
    timeout_s: int = 60
    retries: int = 3
    interval_s: int = 10


class EnvironmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rank: int = Field(ge=0)
    requires_approval: bool = True
    gate_config: GateConfig = Field(default_factory=GateConfig)
    provider_config: DeployProviderConfig = Field(default_factory=DeployProviderConfig)
    health_check: HealthCheckSpec = Field(default_factory=HealthCheckSpec)


class PipelineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    enabled: bool = True
    environments: list[EnvironmentSpec] = Field(min_length=1)

    @field_validator("environments")
    @classmethod
    def _unique_names_and_ranks(cls, value: list[EnvironmentSpec]) -> list[EnvironmentSpec]:
        names = [e.name for e in value]
        ranks = [e.rank for e in value]
        if len(set(names)) != len(names):
            raise ValueError("environment names must be unique within a pipeline")
        if len(set(ranks)) != len(ranks):
            raise ValueError("environment ranks must be unique within a pipeline")
        return value


# --------------------------------------------------------------------------- #
# Gate evaluation                                                              #
# --------------------------------------------------------------------------- #
class GateCheckResult(BaseModel):
    name: GateCheckName
    status: GateCheckStatus
    detail: str = ""
    metrics: dict[str, str] = Field(default_factory=dict)


class GateEvaluation(BaseModel):
    deployment_id: uuid.UUID
    environment: str
    can_proceed: bool
    requires_human_approval: bool
    checks: list[GateCheckResult] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Provider / health                                                           #
# --------------------------------------------------------------------------- #
class DeployRequest(BaseModel):
    deployment_id: uuid.UUID
    repo_id: str
    environment: str
    commit_sha: str
    artifact_ref: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class DeployHandle(BaseModel):
    provider: str
    external_id: str
    url: str | None = None


class DeployStatus(BaseModel):
    state: Literal["pending", "in_progress", "success", "failure", "error"]
    detail: str | None = None
    finished: bool = False


class HealthCheckResult(BaseModel):
    status: HealthStatus
    attempts: int
    detail: str = ""
    log_ref: str | None = None


__all__ = [
    "DeployHandle",
    "DeployProviderConfig",
    "DeployRequest",
    "DeployStatus",
    "EnvironmentSpec",
    "FreezeWindow",
    "GateCheckResult",
    "GateConfig",
    "GateEvaluation",
    "HealthCheckResult",
    "HealthCheckSpec",
    "PipelineSpec",
]
