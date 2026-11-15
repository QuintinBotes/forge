"""Typed errors raised by the deployment subsystem.

The API layer maps these to HTTP status codes (gate/predecessor/freeze -> 409
with ``blocking_reasons``, version -> 409, unknown/cross-workspace -> 404,
restricted-unset / unknown-env -> 422).
"""

from __future__ import annotations


class DeployError(Exception):
    """Base class for all deployment errors."""


class DeploymentNotFoundError(DeployError):
    """A deployment id is absent in the caller's workspace."""


class PipelineNotFoundError(DeployError):
    """No pipeline configured for the project/repo."""


class EnvironmentNotFoundError(DeployError):
    """The named environment is not a stage in the pipeline."""


class GateBlockedError(DeployError):
    """The deployment gate did not clear."""

    def __init__(self, blocking_reasons: list[str]) -> None:
        self.blocking_reasons = list(blocking_reasons)
        super().__init__("; ".join(blocking_reasons) or "gate blocked")


class PredecessorNotReadyError(GateBlockedError):
    """The predecessor environment has not succeeded for the same artifact."""


class FreezeWindowError(GateBlockedError):
    """The environment is in a freeze window."""


class ProviderError(DeployError):
    """A deploy provider failed to trigger or report status."""


class DeploymentConflictError(DeployError):
    """An active deployment already holds the environment's single slot."""


class VersionConflictError(DeployError):
    """A pipeline config edit raced a concurrent edit (stale ``version``)."""

    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"stale pipeline version; current is {current_version}")


class InvalidTransitionError(DeployError):
    """No transition matches the (state, event) pair (guards unmet)."""


class SelfApprovalError(DeployError):
    """The initiator may not approve their own deployment."""


class UnauthorizedApproverError(DeployError):
    """The principal is not in the environment's approver group."""


class RuleValidationError(DeployError):
    """The pipeline config violates the repo ``deploy_rules`` policy."""


class FreezeOverrideNotAllowedError(DeployError):
    """Only an admin may override a freeze window."""


__all__ = [
    "DeployError",
    "DeploymentConflictError",
    "DeploymentNotFoundError",
    "EnvironmentNotFoundError",
    "FreezeOverrideNotAllowedError",
    "FreezeWindowError",
    "GateBlockedError",
    "InvalidTransitionError",
    "PipelineNotFoundError",
    "PredecessorNotReadyError",
    "ProviderError",
    "RuleValidationError",
    "SelfApprovalError",
    "UnauthorizedApproverError",
    "VersionConflictError",
]
