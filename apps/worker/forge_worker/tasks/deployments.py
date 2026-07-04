"""Deployment worker tasks (F31).

Drives the deployment FSM off the Celery ``deployments`` queue so long deploy/
health work cannot starve indexing/verification workers. The deterministic core
helpers (``request_deployment_sync`` / ``advance_deployment_sync``) are testable
without Celery; the ``@celery_app.task`` wrappers are thin prod seams.

No LangGraph — the deployment FSM is deterministic and has no agent loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from forge_contracts.deployment import (
    DeploymentDTO,
    DeploymentRequest,
    DeploymentState,
)
from forge_deploy.errors import DeploymentConflictError
from forge_deploy.gate import CIReader, PolicyReader, SecurityReader, ValidationReader
from forge_deploy.health import HealthChecker, NullHealthChecker
from forge_deploy.orchestrator import DeploymentOrchestrator
from forge_deploy.providers import DeployProvider, NullDeployProvider
from forge_deploy.repository import DeploymentRepository
from forge_worker.celery_app import celery_app

ProviderResolver = Callable[[dict], DeployProvider]
HealthResolver = Callable[[dict], HealthChecker]


def _default_provider_resolver(config: dict) -> DeployProvider:
    return NullDeployProvider()


def _default_health_resolver(config: dict) -> HealthChecker:
    return NullHealthChecker()


def _orchestrator(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    policy: PolicyReader | None,
    ci: CIReader | None,
    validation: ValidationReader | None,
    security: SecurityReader | None,
    provider_resolver: ProviderResolver | None,
    health_resolver: HealthResolver | None,
) -> DeploymentOrchestrator:
    return DeploymentOrchestrator(
        session,
        workspace_id=workspace_id,
        provider_resolver=provider_resolver or _default_provider_resolver,
        health_resolver=health_resolver or _default_health_resolver,
        policy=policy,
        ci=ci,
        validation=validation,
        security=security,
    )


def request_deployment_sync(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: DeploymentRequest,
    initiated_by: str,
    policy: PolicyReader | None = None,
    ci: CIReader | None = None,
    validation: ValidationReader | None = None,
    security: SecurityReader | None = None,
    provider_resolver: ProviderResolver | None = None,
    health_resolver: HealthResolver | None = None,
) -> DeploymentDTO | None:
    """Create a deployment and drive it to a terminal/wait state.

    Returns ``None`` when no pipeline/environment matches the request; raises
    :class:`DeploymentConflictError` if the environment already has an active
    deployment.
    """
    with session_factory() as session:
        repo = DeploymentRepository(session, workspace_id=workspace_id)
        pipeline = repo.get_pipeline_for_project(project_id)
        if pipeline is None:
            return None
        env = repo.get_environment(pipeline.id, request.environment)
        if env is None:
            return None
        if request.idempotency_key:
            existing = repo.find_by_idempotency(env.id, request.idempotency_key)
            if existing is not None:
                return DeploymentDTO.model_validate(existing)
        if repo.active_for_environment(env.id) is not None:
            raise DeploymentConflictError(
                f"an active deployment already exists for {env.name!r}"
            )
        pred = repo.predecessor(env)
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
        dep_id = dep.id
        _orchestrator(
            session,
            workspace_id,
            policy=policy,
            ci=ci,
            validation=validation,
            security=security,
            provider_resolver=provider_resolver,
            health_resolver=health_resolver,
        ).advance(dep_id)
        session.commit()
        return DeploymentDTO.model_validate(repo.get_or_404(dep_id))


def advance_deployment_sync(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID,
    deployment_id: uuid.UUID,
    policy: PolicyReader | None = None,
    ci: CIReader | None = None,
    validation: ValidationReader | None = None,
    security: SecurityReader | None = None,
    provider_resolver: ProviderResolver | None = None,
    health_resolver: HealthResolver | None = None,
) -> DeploymentState:
    """Resume driving a deployment (e.g. after a provider status callback)."""
    with session_factory() as session:
        state = _orchestrator(
            session,
            workspace_id,
            policy=policy,
            ci=ci,
            validation=validation,
            security=security,
            provider_resolver=provider_resolver,
            health_resolver=health_resolver,
        ).advance(deployment_id)
        session.commit()
        return state


class CeleryDeploymentRequester:
    """``DeploymentRequester`` bound to a workspace; runs the create+advance now.

    Foundation note: the slice sketches a fire-and-forget Celery enqueue, but the
    ``DeploymentRequester`` Protocol returns a ``DeploymentDTO``. This concrete
    requester runs synchronously (so callers get the created deployment) and the
    long-running advance is the same deterministic loop the worker task uses.
    """

    def __init__(
        self, session_factory: sessionmaker[Session], *, workspace_id: uuid.UUID
    ) -> None:
        self._sf = session_factory
        self._ws = workspace_id

    def request_promotion(
        self,
        *,
        project_id: uuid.UUID,
        request: DeploymentRequest,
        initiated_by: str,
    ) -> DeploymentDTO | None:
        return request_deployment_sync(
            self._sf,
            workspace_id=self._ws,
            project_id=project_id,
            request=request,
            initiated_by=initiated_by,
        )


def _session_factory() -> sessionmaker[Session]:  # pragma: no cover - prod seam
    from forge_db import create_db_engine, create_session_factory, get_database_url

    return create_session_factory(create_db_engine(get_database_url()))


@celery_app.task(name="forge.deployments.advance", queue="deployments")
def advance_deployment(
    deployment_id: str, workspace_id: str
) -> str:  # pragma: no cover - prod seam
    state = advance_deployment_sync(
        _session_factory(),
        workspace_id=uuid.UUID(workspace_id),
        deployment_id=uuid.UUID(deployment_id),
    )
    return state.value


@celery_app.task(name="forge.deployments.request", queue="deployments")
def request_deployment_task(
    workspace_id: str,
    project_id: str,
    request: dict,
    initiated_by: str,
) -> str | None:  # pragma: no cover - prod seam
    dto = request_deployment_sync(
        _session_factory(),
        workspace_id=uuid.UUID(workspace_id),
        project_id=uuid.UUID(project_id),
        request=DeploymentRequest.model_validate(request),
        initiated_by=initiated_by,
    )
    return str(dto.id) if dto else None


__all__ = [
    "CeleryDeploymentRequester",
    "advance_deployment",
    "advance_deployment_sync",
    "request_deployment_sync",
    "request_deployment_task",
]
