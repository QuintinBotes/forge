"""Deployment-gates contract surface (F31 — deployment gates & promotion).

The frozen, dependency-free DTOs/enums/Protocols the deployment subsystem shares
across packages (``forge_db`` columns, ``forge_deploy`` engine, ``forge_api``
router, ``forge_worker`` tasks). Enums are ``str`` enums so they store as VARCHAR
(``forge_db.base.enum_type``) and serialize verbatim.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


# --------------------------------------------------------------------------- #
# Enums (DB-persisted; mirrored into forge_db.models.enums)                    #
# --------------------------------------------------------------------------- #
class DeploymentState(enum.StrEnum):
    """The 12 states of the deployment promotion FSM."""

    REQUESTED = "requested"
    GATE_EVALUATING = "gate_evaluating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DEPLOYING = "deploying"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    GATE_REJECTED = "gate_rejected"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[DeploymentState] = frozenset(
    {
        DeploymentState.SUCCEEDED,
        DeploymentState.FAILED,
        DeploymentState.GATE_REJECTED,
        DeploymentState.ROLLED_BACK,
        DeploymentState.CANCELLED,
    }
)


class DeploymentEventType(enum.StrEnum):
    """Events that drive a deployment transition."""

    REQUEST = "request"
    GATE_PASSED = "gate_passed"
    GATE_REQUIRES_APPROVAL = "gate_requires_approval"
    GATE_FAILED = "gate_failed"
    APPROVE = "approve"
    REJECT = "reject"
    DEPLOY_STARTED = "deploy_started"
    DEPLOY_SUCCEEDED = "deploy_succeeded"
    DEPLOY_FAILED = "deploy_failed"
    HEALTH_PASSED = "health_passed"
    HEALTH_FAILED = "health_failed"
    ROLLBACK_SUCCEEDED = "rollback_succeeded"
    ROLLBACK_FAILED = "rollback_failed"
    CANCEL = "cancel"


class DeploymentKind(enum.StrEnum):
    PROMOTION = "promotion"
    ROLLBACK = "rollback"
    REDEPLOY = "redeploy"


class DeploymentTrigger(enum.StrEnum):
    MANUAL = "manual"
    AUTO_PROMOTE = "auto_promote"
    AGENT = "agent"
    AUTOMATION = "automation"
    ROLLBACK = "rollback"


class GateCheckName(enum.StrEnum):
    POLICY_ALLOWS = "policy_allows"
    PREDECESSOR_SUCCEEDED = "predecessor_succeeded"
    CI_GREEN = "ci_green"
    SPEC_VALIDATED = "spec_validated"
    SECURITY_CLEAN = "security_clean"
    NOT_FROZEN = "not_frozen"


class GateCheckStatus(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    SKIPPED = "skipped"


class HealthStatus(enum.StrEnum):
    PASSING = "passing"
    FAILING = "failing"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# DTOs                                                                         #
# --------------------------------------------------------------------------- #
class DeploymentRequest(BaseModel):
    """A request to promote an artifact to an environment."""

    model_config = ConfigDict(extra="forbid")

    environment: str
    commit_sha: str
    artifact_ref: str | None = None
    kind: DeploymentKind = DeploymentKind.PROMOTION
    trigger: DeploymentTrigger = DeploymentTrigger.MANUAL
    workflow_run_id: uuid.UUID | None = None
    agent_run_id: uuid.UUID | None = None
    idempotency_key: str | None = None


class DeploymentDTO(BaseModel):
    """A read-model of a single deployment run."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    environment_name: str
    repo_id: str
    commit_sha: str
    artifact_ref: str | None = None
    from_environment_name: str | None = None
    kind: DeploymentKind
    rollback_of: uuid.UUID | None = None
    state: DeploymentState
    trigger: DeploymentTrigger
    initiated_by: str
    provider_name: str | None = None
    provider_url: str | None = None
    health_status: HealthStatus | None = None
    failure_reason: str | None = None
    requested_at: datetime
    finished_at: datetime | None = None


@runtime_checkable
class DeploymentRequester(Protocol):
    """Port board/automation/merge handlers use to request a promotion without
    importing ``forge_deploy``."""

    def request_promotion(
        self,
        *,
        project_id: uuid.UUID,
        request: DeploymentRequest,
        initiated_by: str,
    ) -> DeploymentDTO: ...


__all__ = [
    "TERMINAL_STATES",
    "DeploymentDTO",
    "DeploymentEventType",
    "DeploymentKind",
    "DeploymentRequest",
    "DeploymentRequester",
    "DeploymentState",
    "DeploymentTrigger",
    "GateCheckName",
    "GateCheckStatus",
    "HealthStatus",
]
