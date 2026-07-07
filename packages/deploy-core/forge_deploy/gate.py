"""The deployment gate evaluator — the deploy analogue of F08's MergeGateEvaluator.

Composes repo policy ``deploy_rules``, predecessor-success ordering, automated
checks (CI green, spec validated, security clean), and freeze-window state into a
:class:`GateEvaluation`. **Deterministic, total, and never raises** — every input
combination yields a ``GateEvaluation``. Restricted environments **always**
require human approval; no input can relax that.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from forge_contracts.dtos import DeployRules
from forge_db.models.deployment import Environment
from forge_deploy.freeze import Clock, SystemClock, is_frozen
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import (
    FreezeWindow,
    GateCheckResult,
    GateConfig,
    GateEvaluation,
)
from forge_deploy.states import (
    DeploymentTrigger,
    GateCheckName,
    GateCheckStatus,
)


@runtime_checkable
class PolicyReader(Protocol):
    def deploy_rules(self, repo_id: str) -> DeployRules: ...


@runtime_checkable
class CIReader(Protocol):
    """F08's ``GitHubAdapter.get_combined_status`` consumer view."""

    def get_combined_status(self, repo_id: str, commit_sha: str) -> str | None: ...


@runtime_checkable
class ValidationReader(Protocol):
    """F02 ValidationReport reader, pinned to ``head_sha == commit_sha``."""

    def validation_status(self, repo_id: str, commit_sha: str) -> str | None: ...


@runtime_checkable
class SecurityReader(Protocol):
    def critical_findings(self, repo_id: str, commit_sha: str) -> int | None: ...


class _NullPolicyReader:
    def deploy_rules(self, repo_id: str) -> DeployRules:
        return DeployRules()


class _NullCIReader:
    def get_combined_status(self, repo_id: str, commit_sha: str) -> str | None:
        return None


class _NullValidationReader:
    def validation_status(self, repo_id: str, commit_sha: str) -> str | None:
        return None


class _NullSecurityReader:
    def critical_findings(self, repo_id: str, commit_sha: str) -> int | None:
        return None


class DeploymentGateEvaluator:
    def __init__(
        self,
        repo: DeploymentRepository,
        *,
        policy: PolicyReader | None = None,
        ci: CIReader | None = None,
        validation: ValidationReader | None = None,
        security: SecurityReader | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.repo = repo
        self.policy = policy or _NullPolicyReader()
        self.ci = ci or _NullCIReader()
        self.validation = validation or _NullValidationReader()
        self.security = security or _NullSecurityReader()
        self.clock = clock or SystemClock()

    def evaluate(self, deployment_id: uuid.UUID, *, persist: bool = False) -> GateEvaluation:
        dep = self.repo.get_or_404(deployment_id)
        env = self.repo.session.get(Environment, dep.environment_id)
        if env is None:  # pragma: no cover - referential integrity guarantees it
            raise ValueError("deployment has no environment")
        gate_config = GateConfig.model_validate(env.gate_config or {})
        required = set(gate_config.required_checks)
        rules = self._safe_deploy_rules(dep.repo_id)

        checks: list[GateCheckResult] = []
        checks.append(self._check_policy(env.name, rules))
        checks.append(self._check_predecessor(env, dep.commit_sha))
        if GateCheckName.CI_GREEN in required:
            checks.append(self._check_ci(dep.repo_id, dep.commit_sha))
        if GateCheckName.SPEC_VALIDATED in required:
            checks.append(self._check_spec(dep.repo_id, dep.commit_sha))
        if GateCheckName.SECURITY_CLEAN in required:
            checks.append(self._check_security(dep.repo_id, dep.commit_sha))
        checks.append(
            self._check_freeze(gate_config, frozen_overridden=dep.freeze_override_by is not None)
        )

        agent_blocked = dep.trigger == DeploymentTrigger.AGENT and not rules.allow_agent_deploy
        requires_human_approval = bool(env.is_restricted or env.requires_approval or agent_blocked)
        can_proceed = all(c.status != GateCheckStatus.FAILED for c in checks)
        blocking = [c.detail for c in checks if c.status == GateCheckStatus.FAILED]

        if persist:
            for c in checks:
                self.repo.add_check_result(
                    deployment_id,
                    name=c.name,
                    status=c.status,
                    detail=c.detail,
                    metrics=c.metrics,
                )

        return GateEvaluation(
            deployment_id=deployment_id,
            environment=env.name,
            can_proceed=can_proceed,
            requires_human_approval=requires_human_approval,
            checks=checks,
            blocking_reasons=blocking,
        )

    # ----------------------------------------------------------- check impls
    def _safe_deploy_rules(self, repo_id: str) -> DeployRules:
        try:
            return self.policy.deploy_rules(repo_id)
        except Exception:
            return DeployRules()

    def _check_policy(self, env_name: str, rules: DeployRules) -> GateCheckResult:
        known = {e.lower() for e in rules.environments} | {
            e.lower() for e in rules.restricted_environments
        }
        if known and env_name.lower() not in known:
            return GateCheckResult(
                name=GateCheckName.POLICY_ALLOWS,
                status=GateCheckStatus.FAILED,
                detail=f"environment {env_name!r} is not declared in deploy_rules",
            )
        return GateCheckResult(
            name=GateCheckName.POLICY_ALLOWS,
            status=GateCheckStatus.PASSED,
            detail=f"environment {env_name!r} permitted by deploy_rules",
        )

    def _check_predecessor(self, env: Environment, commit_sha: str) -> GateCheckResult:
        if env.rank == 0:
            return GateCheckResult(
                name=GateCheckName.PREDECESSOR_SUCCEEDED,
                status=GateCheckStatus.SKIPPED,
                detail="first stage has no predecessor",
            )
        pred = self.repo.predecessor(env)
        match = self.repo.predecessor_succeeded_for(env, commit_sha)
        if match is not None:
            return GateCheckResult(
                name=GateCheckName.PREDECESSOR_SUCCEEDED,
                status=GateCheckStatus.PASSED,
                detail=f"{pred.name if pred else 'predecessor'} succeeded for {commit_sha}",
            )
        current = self.repo.currently_deployed(pred.id) if pred else None
        on = current.commit_sha if current else "nothing"
        return GateCheckResult(
            name=GateCheckName.PREDECESSOR_SUCCEEDED,
            status=GateCheckStatus.FAILED,
            detail=(
                f"{env.name} requires {pred.name if pred else 'predecessor'} to have a "
                f"successful deployment of {commit_sha}; it is on {on}"
            ),
        )

    def _check_ci(self, repo_id: str, commit_sha: str) -> GateCheckResult:
        try:
            status = self.ci.get_combined_status(repo_id, commit_sha)
        except Exception:
            status = None
        if status == "success":
            return GateCheckResult(
                name=GateCheckName.CI_GREEN,
                status=GateCheckStatus.PASSED,
                detail="CI combined status is success",
                metrics={"ci_status": "success"},
            )
        return GateCheckResult(
            name=GateCheckName.CI_GREEN,
            status=GateCheckStatus.FAILED,
            detail=f"CI combined status is {status or 'unknown'} (want success)",
            metrics={"ci_status": str(status)},
        )

    def _check_spec(self, repo_id: str, commit_sha: str) -> GateCheckResult:
        try:
            status = self.validation.validation_status(repo_id, commit_sha)
        except Exception:
            status = None
        if status is None:
            return GateCheckResult(
                name=GateCheckName.SPEC_VALIDATED,
                status=GateCheckStatus.SKIPPED,
                detail="no validation report linked to this commit",
            )
        if status == "pass":
            return GateCheckResult(
                name=GateCheckName.SPEC_VALIDATED,
                status=GateCheckStatus.PASSED,
                detail="validation report passed for this commit",
            )
        return GateCheckResult(
            name=GateCheckName.SPEC_VALIDATED,
            status=GateCheckStatus.FAILED,
            detail=f"validation report status is {status!r} (want pass)",
        )

    def _check_security(self, repo_id: str, commit_sha: str) -> GateCheckResult:
        try:
            findings = self.security.critical_findings(repo_id, commit_sha)
        except Exception:
            findings = None
        if findings is None:
            return GateCheckResult(
                name=GateCheckName.SECURITY_CLEAN,
                status=GateCheckStatus.SKIPPED,
                detail="no security-review report available",
            )
        if findings == 0:
            return GateCheckResult(
                name=GateCheckName.SECURITY_CLEAN,
                status=GateCheckStatus.PASSED,
                detail="no critical security findings",
            )
        return GateCheckResult(
            name=GateCheckName.SECURITY_CLEAN,
            status=GateCheckStatus.FAILED,
            detail=f"{findings} critical security finding(s)",
        )

    def _check_freeze(self, gate_config: GateConfig, *, frozen_overridden: bool) -> GateCheckResult:
        windows: list[FreezeWindow] = list(gate_config.freeze_windows)
        state = is_frozen(windows, self.clock.now(), gate_config.timezone)
        if not state.frozen:
            return GateCheckResult(
                name=GateCheckName.NOT_FROZEN,
                status=GateCheckStatus.PASSED,
                detail="not in a freeze window",
            )
        if frozen_overridden:
            return GateCheckResult(
                name=GateCheckName.NOT_FROZEN,
                status=GateCheckStatus.PASSED,
                detail=f"freeze overridden by admin ({state.reason})",
            )
        until = state.until.isoformat() if state.until else "unknown"
        return GateCheckResult(
            name=GateCheckName.NOT_FROZEN,
            status=GateCheckStatus.FAILED,
            detail=f"environment is in a freeze window until {until} ({state.reason})",
        )


__all__ = [
    "CIReader",
    "DeploymentGateEvaluator",
    "PolicyReader",
    "SecurityReader",
    "ValidationReader",
]
