"""Deployment orchestration service (F31).

Owns pipeline upsert (validated against repo ``deploy_rules``), deployment
requests (idempotent, single-active-per-env), the approval decision path
(no-self-approval + distinct ``min_approvals``), cancel, rollback, and freeze
override. All work is workspace-scoped; cross-workspace ids surface as
``DeploymentNotFoundError`` (router -> 404).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal
from forge_api.observability.audit import AuditCategory, AuditLog
from forge_contracts.deployment import (
    DeploymentDTO,
    DeploymentRequest,
)
from forge_contracts.dtos import DeployRules
from forge_contracts.enums import UserRole
from forge_db.models.deployment import Deployment, Environment, EnvironmentPipeline
from forge_deploy.engine import DeploymentStateMachine
from forge_deploy.errors import (
    DeploymentConflictError,
    EnvironmentNotFoundError,
    InvalidTransitionError,
    PipelineNotFoundError,
    SelfApprovalError,
    UnauthorizedApproverError,
    VersionConflictError,
)
from forge_deploy.freeze import Clock, SystemClock
from forge_deploy.gate import (
    CIReader,
    DeploymentGateEvaluator,
    PolicyReader,
    SecurityReader,
    ValidationReader,
)
from forge_deploy.health import HealthChecker, NullHealthChecker
from forge_deploy.orchestrator import DeploymentOrchestrator
from forge_deploy.pipeline import resolve_environments
from forge_deploy.providers import DeployProvider, NullDeployProvider
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import (
    EnvironmentSpec,
    GateConfig,
    GateEvaluation,
    PipelineSpec,
)
from forge_deploy.states import (
    TERMINAL_STATES,
    DeploymentEvent,
    DeploymentEventType,
    DeploymentKind,
    DeploymentState,
    DeploymentTrigger,
)


class NotInitiatorError(Exception):
    """Caller is neither the deployment initiator nor an admin."""


class _DefaultPolicyReader:
    def deploy_rules(self, repo_id: str) -> DeployRules:
        return DeployRules()


class _DefaultCIReader:
    def get_combined_status(self, repo_id: str, commit_sha: str) -> str | None:
        return None


def _default_provider_resolver(config: dict) -> DeployProvider:
    return NullDeployProvider()


def _default_health_resolver(config: dict) -> HealthChecker:
    return NullHealthChecker()


class DeploymentService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        audit: AuditLog | None = None,
        policy_reader: PolicyReader | None = None,
        ci_reader: CIReader | None = None,
        validation_reader: ValidationReader | None = None,
        security_reader: SecurityReader | None = None,
        provider_resolver: Callable[[dict], DeployProvider] | None = None,
        health_resolver: Callable[[dict], HealthChecker] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._sf = session_factory
        self._audit = audit or AuditLog()
        self._policy = policy_reader or _DefaultPolicyReader()
        self._ci = ci_reader or _DefaultCIReader()
        self._validation = validation_reader
        self._security = security_reader
        self._provider_resolver = provider_resolver or _default_provider_resolver
        self._health_resolver = health_resolver or _default_health_resolver
        self._clock = clock or SystemClock()

    # ------------------------------------------------------------- helpers
    def _repo(self, session: Session, ws: uuid.UUID) -> DeploymentRepository:
        return DeploymentRepository(session, workspace_id=ws)

    def _engine(self, session: Session, ws: uuid.UUID) -> DeploymentStateMachine:
        return DeploymentStateMachine(session, workspace_id=ws)

    def _orchestrator(self, session: Session, ws: uuid.UUID) -> DeploymentOrchestrator:
        return DeploymentOrchestrator(
            session,
            workspace_id=ws,
            engine=self._engine(session, ws),
            provider_resolver=self._provider_resolver,
            health_resolver=self._health_resolver,
            policy=self._policy,
            ci=self._ci,
            validation=self._validation,
            security=self._security,
            clock=self._clock,
        )

    @staticmethod
    def _dto(dep: Deployment) -> DeploymentDTO:
        return DeploymentDTO.model_validate(dep)

    def _audit_record(self, action: str, *, ws: uuid.UUID, actor: str, target: str) -> None:
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action=action,
            actor=actor,
            workspace_id=ws,
            target=target,
        )

    # ------------------------------------------------------------ pipeline
    def get_pipeline(self, *, ws: uuid.UUID, project_id: uuid.UUID) -> dict:
        with self._sf() as session:
            repo = self._repo(session, ws)
            pipeline = repo.get_pipeline_for_project(project_id)
            if pipeline is None:
                raise PipelineNotFoundError(str(project_id))
            return self._pipeline_view(repo, pipeline)

    def upsert_pipeline(
        self,
        *,
        ws: uuid.UUID,
        project_id: uuid.UUID,
        repo_id: str,
        enabled: bool,
        version: int,
        environments: list,
        actor: str,
    ) -> dict:
        rules = self._policy.deploy_rules(repo_id)
        requested_restricted = {
            e.name: e.is_restricted for e in environments if e.is_restricted is not None
        }
        spec = PipelineSpec(
            repo_id=repo_id,
            enabled=enabled,
            environments=[
                EnvironmentSpec(
                    name=e.name,
                    rank=e.rank,
                    requires_approval=e.requires_approval,
                    gate_config=e.gate_config,
                    provider_config=e.provider_config,
                    health_check=e.health_check,
                )
                for e in environments
            ],
        )
        resolved = resolve_environments(spec, rules, requested_restricted=requested_restricted)

        with self._sf() as session:
            repo = self._repo(session, ws)
            existing = repo.get_pipeline_for_project(project_id)
            if existing is None:
                existing = EnvironmentPipeline(
                    workspace_id=ws,
                    project_id=project_id,
                    repo_id=repo_id,
                    enabled=enabled,
                    version=1,
                )
                session.add(existing)
                session.flush()
            else:
                if existing.version != version:
                    raise VersionConflictError(existing.version)
                existing.repo_id = repo_id
                existing.enabled = enabled
                existing.version += 1
            self._sync_environments(session, ws, existing, resolved)
            session.commit()
            self._audit_record(
                "deployment.pipeline.upsert",
                ws=ws,
                actor=actor,
                target=f"pipeline:{existing.id}",
            )
            repo = self._repo(session, ws)
            pipeline = repo.get_pipeline_for_project(project_id)
            if pipeline is None:  # pragma: no cover - just upserted above
                raise PipelineNotFoundError(str(project_id))
            return self._pipeline_view(repo, pipeline)

    def _sync_environments(
        self,
        session: Session,
        ws: uuid.UUID,
        pipeline: EnvironmentPipeline,
        resolved: list,
    ) -> None:
        existing = {e.name: e for e in pipeline.environments}
        # Offset existing ranks to avoid transient unique(rank) collisions.
        for existing_env in existing.values():
            existing_env.rank += 1000
        session.flush()
        seen: set[str] = set()
        for r in resolved:
            seen.add(r.name)
            env = existing.get(r.name)
            if env is None:
                env = Environment(workspace_id=ws, pipeline_id=pipeline.id, name=r.name)
                session.add(env)
            env.rank = r.rank
            env.is_restricted = r.is_restricted
            env.requires_approval = r.requires_approval
            env.gate_config = r.gate_config
            env.provider_config = r.provider_config
            env.health_check = r.health_check
        for name, env in existing.items():
            if name not in seen:
                session.delete(env)
        session.flush()

    def _pipeline_view(self, repo: DeploymentRepository, pipeline: EnvironmentPipeline) -> dict:
        envs = []
        for env in repo.environments(pipeline.id):
            current = repo.currently_deployed(env.id)
            envs.append(
                {
                    "id": env.id,
                    "name": env.name,
                    "rank": env.rank,
                    "is_restricted": env.is_restricted,
                    "requires_approval": env.requires_approval,
                    "gate_config": env.gate_config,
                    "provider_config": env.provider_config,
                    "health_check": env.health_check,
                    "currently_deployed": self._dto(current) if current else None,
                }
            )
        return {
            "id": pipeline.id,
            "project_id": pipeline.project_id,
            "repo_id": pipeline.repo_id,
            "enabled": pipeline.enabled,
            "version": pipeline.version,
            "environments": envs,
        }

    # ---------------------------------------------------------- deployments
    def request_deployment(
        self,
        *,
        ws: uuid.UUID,
        project_id: uuid.UUID,
        request: DeploymentRequest,
        initiated_by: str,
    ) -> DeploymentDTO:
        with self._sf() as session:
            repo = self._repo(session, ws)
            pipeline = repo.get_pipeline_for_project(project_id)
            if pipeline is None:
                raise PipelineNotFoundError(str(project_id))
            env = repo.get_environment(pipeline.id, request.environment)
            if env is None:
                raise EnvironmentNotFoundError(request.environment)
            if request.idempotency_key:
                existing = repo.find_by_idempotency(env.id, request.idempotency_key)
                if existing is not None:
                    return self._dto(existing)
            if repo.active_for_environment(env.id) is not None:
                raise DeploymentConflictError(
                    f"an active deployment already exists for {env.name!r}"
                )
            pred = repo.predecessor(env)
            try:
                dep = repo.create_deployment(
                    project_id=project_id,
                    pipeline_id=pipeline.id,
                    environment_id=env.id,
                    environment_name=env.name,
                    repo_id=pipeline.repo_id,
                    commit_sha=request.commit_sha,
                    artifact_ref=request.artifact_ref,
                    from_environment_name=pred.name if pred else None,
                    kind=request.kind,
                    trigger=request.trigger,
                    initiated_by=initiated_by,
                    workflow_run_id=request.workflow_run_id,
                    agent_run_id=request.agent_run_id,
                    idempotency_key=request.idempotency_key,
                    state=DeploymentState.REQUESTED,
                )
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                raise DeploymentConflictError(str(exc)) from exc
            dep_id = dep.id
            self._orchestrator(session, ws).advance(dep_id)
            session.commit()
            self._audit_record(
                "deployment.request",
                ws=ws,
                actor=initiated_by,
                target=f"deployment:{dep_id}",
            )
            return self._dto(self._repo(session, ws).get_or_404(dep_id))

    def request_promotion(
        self,
        *,
        ws: uuid.UUID,
        project_id: uuid.UUID,
        request: DeploymentRequest,
        initiated_by: str,
    ) -> DeploymentDTO:
        """``DeploymentRequester`` entrypoint (board/automation/merge handlers)."""
        return self.request_deployment(
            ws=ws,
            project_id=project_id,
            request=request,
            initiated_by=initiated_by,
        )

    def auto_promote_on_merge(
        self,
        *,
        ws: uuid.UUID,
        project_id: uuid.UUID,
        repo_id: str,
        commit_sha: str,
    ) -> DeploymentDTO | None:
        """Auto-request a promotion to the first stage when it opts in (AC21).

        Returns the created/existing deployment, or ``None`` when no pipeline is
        configured for the repo or the first stage has ``auto_promote_on_merge``
        disabled. Idempotent on ``{env}:{commit_sha}``.
        """
        with self._sf() as session:
            repo = self._repo(session, ws)
            pipeline = repo.get_pipeline_for_project(project_id)
            if pipeline is None or not pipeline.enabled or pipeline.repo_id != repo_id:
                return None
            envs = repo.environments(pipeline.id)
            if not envs:
                return None
            first = envs[0]
            cfg = GateConfig.model_validate(first.gate_config or {})
            if not cfg.auto_promote_on_merge:
                return None
            first_name = first.name
        return self.request_deployment(
            ws=ws,
            project_id=project_id,
            request=DeploymentRequest(
                environment=first_name,
                commit_sha=commit_sha,
                trigger=DeploymentTrigger.AUTO_PROMOTE,
                idempotency_key=f"{first_name}:{commit_sha}",
            ),
            initiated_by="system:auto_promote",
        )

    def list_deployments(
        self,
        *,
        ws: uuid.UUID,
        project_id: uuid.UUID,
        environment: str | None = None,
        state: DeploymentState | None = None,
        limit: int = 50,
    ) -> list[DeploymentDTO]:
        with self._sf() as session:
            repo = self._repo(session, ws)
            rows = repo.list_deployments(
                project_id=project_id,
                environment_name=environment,
                state=state,
                limit=limit,
            )
            return [self._dto(r) for r in rows]

    def get_deployment(self, *, ws: uuid.UUID, deployment_id: uuid.UUID) -> dict:
        with self._sf() as session:
            repo = self._repo(session, ws)
            dep = repo.get_or_404(deployment_id)
            checks = repo.checks(deployment_id)
            transitions = repo.transitions(deployment_id)
            detail = self._dto(dep).model_dump()
            detail["checks"] = [
                {
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                    "metrics": c.metrics,
                }
                for c in checks
            ]
            detail["transitions"] = [
                {
                    "sequence": t.sequence,
                    "from_state": t.from_state,
                    "to_state": t.to_state,
                    "event": t.event,
                    "actor": t.actor,
                    "created_at": t.created_at,
                }
                for t in transitions
            ]
            detail["gate"] = None
            detail["diff_since"] = None
            return detail

    def get_gate(self, *, ws: uuid.UUID, deployment_id: uuid.UUID) -> GateEvaluation:
        with self._sf() as session:
            repo = self._repo(session, ws)
            repo.get_or_404(deployment_id)
            evaluator = DeploymentGateEvaluator(
                repo,
                policy=self._policy,
                ci=self._ci,
                validation=self._validation,
                security=self._security,
                clock=self._clock,
            )
            return evaluator.evaluate(deployment_id, persist=False)

    # ------------------------------------------------------------- decision
    def decide(
        self,
        *,
        ws: uuid.UUID,
        deployment_id: uuid.UUID,
        decision: str,
        principal: Principal,
        note: str | None = None,
    ) -> DeploymentDTO:
        with self._sf() as session:
            repo = self._repo(session, ws)
            dep = repo.get_or_404(deployment_id)
            if dep.state != DeploymentState.AWAITING_APPROVAL:
                raise InvalidTransitionError(
                    f"deployment is not awaiting approval (state={dep.state.value})"
                )
            env = session.get(Environment, dep.environment_id)
            self._authorize_approver(dep, env, principal)
            actor = f"user:{principal.user_id}"
            engine = self._engine(session, ws)
            if decision == "reject":
                engine.transition(
                    deployment_id,
                    DeploymentEvent(type=DeploymentEventType.REJECT, actor=actor),
                )
            elif decision == "approve":
                try:
                    repo.add_approval(
                        deployment_id,
                        approver_user_id=principal.user_id,
                        decision="approve",
                        reason=note,
                    )
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    dep = repo.get_or_404(deployment_id)
                cfg = GateConfig.model_validate((env.gate_config if env else {}) or {})
                if repo.distinct_approver_count(deployment_id) >= max(1, cfg.min_approvals):
                    engine.transition(
                        deployment_id,
                        DeploymentEvent(type=DeploymentEventType.APPROVE, actor=actor),
                    )
                    self._orchestrator(session, ws).advance(deployment_id)
            else:
                raise InvalidTransitionError(f"unknown decision {decision!r}")
            session.commit()
            self._audit.record(
                category=AuditCategory.APPROVAL,
                action=f"deployment.{decision}",
                actor=actor,
                workspace_id=ws,
                target=f"deployment:{deployment_id}",
            )
            return self._dto(self._repo(session, ws).get_or_404(deployment_id))

    def _authorize_approver(
        self, dep: Deployment, env: Environment | None, principal: Principal
    ) -> None:
        actor = f"user:{principal.user_id}"
        agent_actor = f"agent:{principal.user_id}"
        if dep.initiated_by in {actor, agent_actor}:
            raise SelfApprovalError("the initiator cannot approve their own deployment")
        if env is not None:
            cfg = GateConfig.model_validate(env.gate_config or {})
            if cfg.approver_user_ids and principal.user_id not in cfg.approver_user_ids:
                raise UnauthorizedApproverError(
                    "principal is not in the environment approver group"
                )

    # --------------------------------------------------------------- cancel
    def cancel(
        self, *, ws: uuid.UUID, deployment_id: uuid.UUID, principal: Principal
    ) -> DeploymentDTO:
        with self._sf() as session:
            repo = self._repo(session, ws)
            dep = repo.get_or_404(deployment_id)
            if dep.state in TERMINAL_STATES:
                raise InvalidTransitionError("deployment is already terminal")
            actor = f"user:{principal.user_id}"
            if dep.initiated_by != actor and principal.role != UserRole.ADMIN:
                raise NotInitiatorError("only the initiator or an admin can cancel a deployment")
            self._engine(session, ws).transition(
                deployment_id,
                DeploymentEvent(type=DeploymentEventType.CANCEL, actor=actor),
            )
            session.commit()
            self._audit_record(
                "deployment.cancel",
                ws=ws,
                actor=actor,
                target=f"deployment:{deployment_id}",
            )
            return self._dto(self._repo(session, ws).get_or_404(deployment_id))

    # ------------------------------------------------------------- rollback
    def rollback(
        self, *, ws: uuid.UUID, deployment_id: uuid.UUID, principal: Principal
    ) -> DeploymentDTO:
        with self._sf() as session:
            repo = self._repo(session, ws)
            dep = repo.get_or_404(deployment_id)
            target = repo.last_good(dep.environment_id, exclude_id=dep.id)
            if target is None:
                raise InvalidTransitionError("no last-good artifact to roll back to")
            rb = repo.create_deployment(
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
                trigger=DeploymentTrigger.ROLLBACK,
                initiated_by=f"user:{principal.user_id}",
                state=DeploymentState.APPROVED,
            )
            session.flush()
            repo.append_transition(
                rb,
                from_state=DeploymentState.REQUESTED.value,
                to_state=DeploymentState.APPROVED.value,
                event="rollback_initiated",
                actor=f"user:{principal.user_id}",
                effects=["trigger_deploy"],
            )
            rb_id = rb.id
            self._orchestrator(session, ws).advance(rb_id)
            session.commit()
            self._audit_record(
                "deployment.rollback",
                ws=ws,
                actor=f"user:{principal.user_id}",
                target=f"deployment:{rb_id}",
            )
            return self._dto(self._repo(session, ws).get_or_404(rb_id))

    # ------------------------------------------------------- freeze override
    def override_freeze(
        self,
        *,
        ws: uuid.UUID,
        deployment_id: uuid.UUID,
        principal: Principal,
        reason: str,
    ) -> DeploymentDTO:
        with self._sf() as session:
            repo = self._repo(session, ws)
            dep = repo.get_or_404(deployment_id)
            env = session.get(Environment, dep.environment_id)
            if env is None:
                raise EnvironmentNotFoundError(dep.environment_name)
            if repo.active_for_environment(env.id) is not None:
                raise DeploymentConflictError(
                    f"an active deployment already exists for {env.name!r}"
                )
            new = repo.create_deployment(
                project_id=dep.project_id,
                pipeline_id=dep.pipeline_id,
                environment_id=dep.environment_id,
                environment_name=dep.environment_name,
                repo_id=dep.repo_id,
                commit_sha=dep.commit_sha,
                artifact_ref=dep.artifact_ref,
                from_environment_name=dep.from_environment_name,
                kind=DeploymentKind.PROMOTION,
                trigger=dep.trigger,
                initiated_by=f"user:{principal.user_id}",
                freeze_override_by=principal.user_id,
                state=DeploymentState.REQUESTED,
            )
            session.flush()
            new_id = new.id
            repo.append_transition(
                new,
                from_state=DeploymentState.REQUESTED.value,
                to_state=DeploymentState.REQUESTED.value,
                event="freeze_override",
                actor=f"user:{principal.user_id}",
                payload={"reason": reason, "overrides": str(deployment_id)},
            )
            self._orchestrator(session, ws).advance(new_id)
            session.commit()
            self._audit_record(
                "deployment.freeze_override",
                ws=ws,
                actor=f"user:{principal.user_id}",
                target=f"deployment:{new_id}",
            )
            return self._dto(self._repo(session, ws).get_or_404(new_id))


__all__ = ["DeploymentService", "NotInitiatorError"]
