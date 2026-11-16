"""SCF deployment gates & environment promotion (F31).

The deterministic, Postgres-backed deployment control plane: an ordered
environment pipeline, a deployment state machine, a gate evaluator (policy +
predecessor ordering + CI/spec/security checks + freeze windows), pluggable
deploy providers, health checks, and an auto-rollback path. All side effects go
through injected ports so the engine is deterministic and unit-testable.
"""

from __future__ import annotations

from scf_deploy.effects import (
    KNOWN_EFFECTS,
    EffectDispatcher,
    NullEffectDispatcher,
    RecordingEffectDispatcher,
)
from scf_deploy.engine import (
    DeploymentDefinition,
    DeploymentStateMachine,
    DeploymentTransitionRule,
    load_deployment_definition,
    parse_deployment_definition,
)
from scf_deploy.errors import (
    DeployError,
    DeploymentConflictError,
    DeploymentNotFoundError,
    EnvironmentNotFoundError,
    FreezeOverrideNotAllowedError,
    FreezeWindowError,
    GateBlockedError,
    InvalidTransitionError,
    PipelineNotFoundError,
    PredecessorNotReadyError,
    ProviderError,
    RuleValidationError,
    SelfApprovalError,
    UnauthorizedApproverError,
    VersionConflictError,
)
from scf_deploy.freeze import (
    Clock,
    FakeClock,
    FreezeState,
    SystemClock,
    is_frozen,
    next_open,
)
from scf_deploy.gate import (
    CIReader,
    DeploymentGateEvaluator,
    PolicyReader,
    SecurityReader,
    ValidationReader,
)
from scf_deploy.guards import GuardContext, default_guard_registry
from scf_deploy.health import (
    CommandHealthChecker,
    HealthChecker,
    HttpHealthChecker,
    NullHealthChecker,
    ScriptedHealthChecker,
)
from scf_deploy.orchestrator import DeploymentOrchestrator
from scf_deploy.pipeline import (
    PipelineResolver,
    ResolvedEnvironment,
    resolve_environments,
)
from scf_deploy.providers import (
    DeployProvider,
    GitHubActionsProvider,
    GitHubDeploymentsProvider,
    NullDeployProvider,
    WebhookCommandProvider,
)
from scf_deploy.repository import DeploymentRepository
from scf_deploy.schemas import (
    DeployHandle,
    DeployProviderConfig,
    DeployRequest,
    DeployStatus,
    EnvironmentSpec,
    FreezeWindow,
    GateCheckResult,
    GateConfig,
    GateEvaluation,
    HealthCheckResult,
    HealthCheckSpec,
    PipelineSpec,
)
from scf_deploy.states import (
    TERMINAL_STATES,
    DeploymentEvent,
    DeploymentEventType,
    DeploymentKind,
    DeploymentState,
    DeploymentTrigger,
    GateCheckName,
    GateCheckStatus,
    HealthStatus,
)

__version__ = "0.1.0"

__all__ = [
    "KNOWN_EFFECTS",
    "TERMINAL_STATES",
    "CIReader",
    "Clock",
    "CommandHealthChecker",
    "DeployError",
    "DeployHandle",
    "DeployProvider",
    "DeployProviderConfig",
    "DeployRequest",
    "DeployStatus",
    "DeploymentConflictError",
    "DeploymentDefinition",
    "DeploymentEvent",
    "DeploymentEventType",
    "DeploymentGateEvaluator",
    "DeploymentKind",
    "DeploymentNotFoundError",
    "DeploymentOrchestrator",
    "DeploymentRepository",
    "DeploymentState",
    "DeploymentStateMachine",
    "DeploymentTransitionRule",
    "DeploymentTrigger",
    "EffectDispatcher",
    "EnvironmentNotFoundError",
    "EnvironmentSpec",
    "FakeClock",
    "FreezeOverrideNotAllowedError",
    "FreezeState",
    "FreezeWindow",
    "FreezeWindowError",
    "GateBlockedError",
    "GateCheckName",
    "GateCheckResult",
    "GateCheckStatus",
    "GateConfig",
    "GateEvaluation",
    "GitHubActionsProvider",
    "GitHubDeploymentsProvider",
    "GuardContext",
    "HealthCheckResult",
    "HealthCheckSpec",
    "HealthChecker",
    "HealthStatus",
    "HttpHealthChecker",
    "InvalidTransitionError",
    "NullDeployProvider",
    "NullEffectDispatcher",
    "NullHealthChecker",
    "PipelineNotFoundError",
    "PipelineResolver",
    "PipelineSpec",
    "PolicyReader",
    "PredecessorNotReadyError",
    "ProviderError",
    "RecordingEffectDispatcher",
    "ResolvedEnvironment",
    "RuleValidationError",
    "ScriptedHealthChecker",
    "SecurityReader",
    "SelfApprovalError",
    "SystemClock",
    "UnauthorizedApproverError",
    "ValidationReader",
    "VersionConflictError",
    "WebhookCommandProvider",
    "default_guard_registry",
    "is_frozen",
    "load_deployment_definition",
    "next_open",
    "parse_deployment_definition",
    "resolve_environments",
]
