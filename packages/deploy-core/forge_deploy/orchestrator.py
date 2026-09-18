"""Synchronous deployment driver.

Drives a deployment through the FSM by performing the side effect appropriate to
the current state and emitting the next event, until a terminal or wait state
(``awaiting_approval``, pending provider) is reached. Mirrors F08's
``advance_workflow`` re-enqueue loop, but synchronous (the foundation is sync).
Both the API service and the Celery worker use it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from forge_db.models.deployment import Deployment, Environment
from forge_deploy.engine import DeploymentStateMachine
from forge_deploy.freeze import Clock, SystemClock
from forge_deploy.gate import (
    CIReader,
    DeploymentGateEvaluator,
    PolicyReader,
    SecurityReader,
    ValidationReader,
)
from forge_deploy.health import HealthChecker, NullHealthChecker
from forge_deploy.providers import DeployProvider, NullDeployProvider
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import DeployRequest, HealthCheckSpec
from forge_deploy.states import (
    TERMINAL_STATES,
    DeploymentEvent,
    DeploymentEventType,
    DeploymentKind,
    DeploymentState,
    DeploymentTrigger,
    HealthStatus,
)

ProviderResolver = Callable[[dict], DeployProvider]
HealthResolver = Callable[[dict], HealthChecker]


def _default_provider_resolver(config: dict) -> DeployProvider:
    if config.get("provider", "null") == "null":
        return NullDeployProvider()
    raise RuntimeError(f"no provider client configured for {config.get('provider')!r}")


def _default_health_resolver(config: dict) -> HealthChecker:
    return NullHealthChecker()


class DeploymentOrchestrator:
    def __init__(
        self,
        session: Session,
        *,
        workspace_id: uuid.UUID,
        engine: DeploymentStateMachine | None = None,
        provider_resolver: ProviderResolver | None = None,
        health_resolver: HealthResolver | None = None,
        policy: PolicyReader | None = None,
        ci: CIReader | None = None,
        validation: ValidationReader | None = None,
        security: SecurityReader | None = None,
        clock: Clock | None = None,
        max_steps: int = 64,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.repo = DeploymentRepository(session, workspace_id=workspace_id)
        self.engine = engine or DeploymentStateMachine(session, workspace_id=workspace_id)
        self.provider_resolver = provider_resolver or _default_provider_resolver
        self.health_resolver = health_resolver or _default_health_resolver
        self.clock = clock or SystemClock()
        self.evaluator = DeploymentGateEvaluator(
            self.repo,
            policy=policy,
            ci=ci,
            validation=validation,
            security=security,
            clock=self.clock,
        )
        self.max_steps = max_steps

    # ------------------------------------------------------------------ drive
    def advance(self, deployment_id: uuid.UUID) -> DeploymentState:
        for _ in range(self.max_steps):
            dep = self.repo.get_or_404(deployment_id)
            state = dep.state
            if state in TERMINAL_STATES:
                break
            if state == DeploymentState.REQUESTED:
                self.engine.transition(dep.id, DeploymentEvent(type=DeploymentEventType.REQUEST))
            elif state == DeploymentState.GATE_EVALUATING:
                if not self._evaluate_gate(dep):
                    break  # awaiting human approval
            elif state == DeploymentState.AWAITING_APPROVAL:
                break
            elif state == DeploymentState.APPROVED:
                self._trigger_deploy(dep)
            elif state == DeploymentState.DEPLOYING:
                if not self._poll_deploy(dep):
                    break  # provider pending; await callback/poll
            elif state == DeploymentState.VERIFYING:
                self._run_health(dep)
            elif state == DeploymentState.ROLLING_BACK:
                self._do_rollback(dep)
            else:  # pragma: no cover - defensive
                break
        return self.repo.get_or_404(deployment_id).state

    # ----------------------------------------------------------------- steps
    def _evaluate_gate(self, dep: Deployment) -> bool:
        ev = self.evaluator.evaluate(dep.id, persist=True)
        if not ev.can_proceed:
            dep.failure_reason = "; ".join(ev.blocking_reasons) or "gate blocked"
            self.engine.transition(
                dep.id,
                DeploymentEvent(
                    type=DeploymentEventType.GATE_FAILED,
                    payload={"blocking_reasons": ev.blocking_reasons},
                ),
            )
            return True
        if ev.requires_human_approval:
            self.engine.transition(
                dep.id, DeploymentEvent(type=DeploymentEventType.GATE_REQUIRES_APPROVAL)
            )
            return False
        self.engine.transition(dep.id, DeploymentEvent(type=DeploymentEventType.GATE_PASSED))
        return True

    def _env_for(self, dep: Deployment) -> Environment | None:
        return self.session.get(Environment, dep.environment_id)

    def _trigger_deploy(self, dep: Deployment) -> None:
        env = self._env_for(dep)
        config = dict(env.provider_config or {}) if env else {}
        provider = self.provider_resolver(config)
        req = DeployRequest(
            deployment_id=dep.id,
            repo_id=dep.repo_id,
            environment=dep.environment_name,
            commit_sha=dep.commit_sha,
            artifact_ref=dep.artifact_ref,
            config=config,
        )
        handle = provider.trigger(req)
        dep.provider_name = handle.provider
        dep.provider_external_id = handle.external_id
        dep.provider_url = handle.url
        self.session.flush()
        self.engine.transition(dep.id, DeploymentEvent(type=DeploymentEventType.DEPLOY_STARTED))

    def _poll_deploy(self, dep: Deployment) -> bool:
        from forge_deploy.schemas import DeployHandle

        env = self._env_for(dep)
        config = dict(env.provider_config or {}) if env else {}
        provider = self.provider_resolver(config)
        handle = DeployHandle(
            provider=dep.provider_name or provider.name,
            external_id=dep.provider_external_id or "",
            url=dep.provider_url,
        )
        status = provider.get_status(handle)
        if not status.finished:
            return False
        if status.state == "success":
            self.engine.transition(
                dep.id, DeploymentEvent(type=DeploymentEventType.DEPLOY_SUCCEEDED)
            )
        else:
            dep.failure_reason = status.detail or f"deploy {status.state}"
            self.engine.transition(
                dep.id,
                DeploymentEvent(
                    type=DeploymentEventType.DEPLOY_FAILED,
                    payload={"detail": status.detail},
                ),
            )
        return True

    def _run_health(self, dep: Deployment) -> None:
        env = self._env_for(dep)
        spec = HealthCheckSpec.model_validate((env.health_check if env else {}) or {})
        checker = self.health_resolver(env.health_check if env else {})
        result = checker.check(spec, deployment_id=dep.id)
        dep.health_status = result.status
        self.session.flush()
        if result.status == HealthStatus.PASSING:
            self.engine.transition(dep.id, DeploymentEvent(type=DeploymentEventType.HEALTH_PASSED))
        else:
            dep.failure_reason = f"health check failed: {result.detail}"
            self.engine.transition(
                dep.id,
                DeploymentEvent(
                    type=DeploymentEventType.HEALTH_FAILED,
                    payload={"detail": result.detail},
                ),
            )

    def _do_rollback(self, dep: Deployment) -> None:
        target = self.repo.last_good(dep.environment_id, exclude_id=dep.id)
        if target is None:
            dep.failure_reason = "no last-good artifact to roll back to"
            self.engine.transition(
                dep.id, DeploymentEvent(type=DeploymentEventType.ROLLBACK_FAILED)
            )
            return
        rollback = self.repo.create_deployment(
            project_id=dep.project_id,
            pipeline_id=dep.pipeline_id,
            environment_id=dep.environment_id,
            environment_name=dep.environment_name,
            repo_id=dep.repo_id,
            commit_sha=target.commit_sha,
            artifact_ref=target.artifact_ref,
            from_environment_name=dep.from_environment_name,
            kind=DeploymentKind.ROLLBACK,
            rollback_of=dep.id,
            state=DeploymentState.APPROVED,
            trigger=DeploymentTrigger.ROLLBACK,
            initiated_by="system:rollback",
        )
        # Record the rollback start as its first transition (audit).
        self.repo.append_transition(
            rollback,
            from_state=DeploymentState.REQUESTED.value,
            to_state=DeploymentState.APPROVED.value,
            event="rollback_initiated",
            actor="system",
            effects=["trigger_deploy"],
        )
        final = self.advance(rollback.id)
        if final == DeploymentState.SUCCEEDED:
            self.engine.transition(
                dep.id, DeploymentEvent(type=DeploymentEventType.ROLLBACK_SUCCEEDED)
            )
        else:
            dep.failure_reason = "rollback deployment did not succeed"
            self.engine.transition(
                dep.id, DeploymentEvent(type=DeploymentEventType.ROLLBACK_FAILED)
            )


__all__ = ["DeploymentOrchestrator", "HealthResolver", "ProviderResolver"]
