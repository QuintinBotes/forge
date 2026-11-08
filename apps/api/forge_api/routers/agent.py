"""Persist and enqueue agent objectives, read runs, and replay/fork recordings."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_agent import AgentRunner
from forge_agent.providers import (
    ModelClientConfig,
    ModelClientError,
    ModelClientUnavailable,
    build_model_client,
)
from forge_agent.replay import RunCassette
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_api.auth.rbac import Permission
from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.observability.redaction import redact_text
from forge_api.routers._rbac import require_permission
from forge_api.schemas.fork import ForkRunRequest, ForkRunResponse
from forge_api.schemas.pm import PMDispatchConfig
from forge_api.schemas.replay import (
    ReplayCallDiffOut,
    ReplayDivergenceOut,
    ReplayRunRequest,
    ReplayRunResponse,
)
from forge_api.services.agent_dispatch import (
    SERVER_OWNED_CONTEXT,  # noqa: F401  (re-export: historical import path)
    AgentEnqueueFailed,
    RepoNotConnected,
    TrackerRefNotFound,
    build_tracker_objective,
    create_and_enqueue_run,
    resolve_tracker_ref,
)
from forge_api.services.fork_service import (
    PromptOverrideModelClient,
    fork_recording,
)
from forge_api.services.replay_service import replay_recording
from forge_api.settings import get_settings
from forge_contracts import AgentObjective, AgentRunResult, ModelClient
from forge_db.models import AgentRun, RunRecording
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_integrations.pm.errors import ExternalNotFound
from forge_policy import (
    SkillProfileNotAllowedError,
    enforce_skill_profile_allowed,
    load_policy,
)

logger = logging.getLogger("forge_api.routers.agent")

router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(get_current_principal)],
)

# Authorization: starting a run requires RUN_AGENT (member / agent-runner /
# admin); a read-only viewer is denied. Reading a run requires READ.
RunnerPrincipalDep = Annotated[Principal, Depends(require_permission(Permission.RUN_AGENT))]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
SessionDep = Annotated[Session, Depends(get_db)]


# --------------------------------------------------------------------------- #
# Durable run store                                                            #
# --------------------------------------------------------------------------- #


class AgentRunStore:
    """Database-backed runs; also serves the Slack status reader."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def put(self, result: AgentRunResult, *, workspace_id: uuid.UUID) -> AgentRunResult:
        if result.run_id is None:
            result.run_id = uuid.uuid4()
        self.session.add(
            AgentRun(
                id=result.run_id,
                workspace_id=workspace_id,
                status=DbRunStatus(result.status.value),
                steps=[step.model_dump(mode="json") for step in result.steps],
                output=result.model_dump(mode="json"),
                confidence=result.confidence,
            )
        )
        self.session.commit()
        return result

    def get(self, run_id: uuid.UUID, *, workspace_id: uuid.UUID) -> AgentRunResult | None:
        row = self.session.scalar(
            select(AgentRun).where(AgentRun.id == run_id, AgentRun.workspace_id == workspace_id)
        )
        if row is None:
            return None
        return AgentRunResult.model_validate(
            {
                **(row.output or {}),
                "run_id": row.id,
                "task_id": row.task_id
                or row.inputs.get("task_id")
                or (row.output or {}).get("task_id"),
                "status": row.status.value,
                "steps": row.steps,
                "confidence": row.confidence,
            }
        )

    def owner_of(self, run_id: uuid.UUID) -> uuid.UUID | None:
        """Return the workspace that owns ``run_id``, or ``None`` if unknown.

        Read-only, cross-tenant-safe resolution mirroring
        :meth:`~forge_api.routers.integration.ApprovalStore.owner_of`: the Slack
        ``/forge status`` slash command
        is unauthenticated untrusted intake (it carries no Forge principal), so
        the handler resolves the owning workspace from the run id and then reads
        the run back through the normal workspace-scoped :meth:`get`. An unknown
        id yields ``None`` -> the handler returns its not-found copy.
        """
        return self.session.scalar(select(AgentRun.workspace_id).where(AgentRun.id == run_id))


# --------------------------------------------------------------------------- #
# Runner + store dependencies (overridable for tests / BYOK swap)             #
# --------------------------------------------------------------------------- #


#: Raised when a run is requested on a deployment that cannot call a model.
_NO_PROVIDER_DETAIL = (
    "no model provider is configured, so this run cannot be executed. Set "
    "FORGE_MODEL_PROVIDER (anthropic|openai) and a key "
    "(FORGE_MODEL_API_KEY, or the provider-native ANTHROPIC_API_KEY / "
    "OPENAI_API_KEY), or store one in the workspace vault. Note that the dev "
    "stack reads deploy/.env.dev, not the repo-root .env. To run offline with "
    "canned output instead, set FORGE_ALLOW_SCRIPTED_AGENT=1 — never where a "
    "run's result is trusted."
)


def _scripted_fallback_runner() -> AgentRunner:
    """The offline deterministic runner, only when explicitly opted in.

    Carries no confidence: the previous default attached ``confidence=0.9`` to a
    canned string, asserting near-certainty about a result that involved no
    reasoning at all.
    """
    model = ScriptedModelClient(
        responses=[],
        default=finish_response(
            "Offline scripted agent (FORGE_ALLOW_SCRIPTED_AGENT=1): no model was "
            "called and no work was performed.",
        ),
    )
    return AgentRunner(model)


def _default_runner() -> AgentRunner:
    """Build the runner, or refuse when this deployment cannot call a model.

    A run that cannot reach a model has not succeeded, and must not report that
    it has. The previous default silently substituted the scripted client, whose
    every objective "finishes cleanly" — so a run that did nothing was
    indistinguishable downstream from one that did the work.

    The offline client is still available, but only behind an explicit
    ``FORGE_ALLOW_SCRIPTED_AGENT``, so it can never be reached by accident.
    """
    config = ModelClientConfig.from_env()
    if config is not None:
        try:
            return AgentRunner(build_model_client(config, redactor=redact_text))
        except ModelClientUnavailable as exc:
            # The lane IS configured and its SDK is missing — a deployment
            # error, not an invitation to fake the result.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"model provider {config.provider!r} is configured but its SDK is "
                    f"not installed: {exc}"
                ),
            ) from exc
        except ModelClientError as exc:
            # Same class of deployment error, different cause: the provider is
            # configured but the client cannot be built from that config at all
            # — e.g. a local-CLI bridge URL with a blank
            # `FORGE_CLI_BRIDGE_TOKEN`. Without this branch that misconfigured
            # env var surfaced as an unhandled 500 instead of the documented,
            # actionable 503 (final review, I1).
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"model provider {config.provider!r} is configured but a client "
                    f"could not be built from it: {exc}"
                ),
            ) from exc

    if get_settings().allow_scripted_agent:
        logger.warning(
            "FORGE_ALLOW_SCRIPTED_AGENT is set and no model provider is configured: "
            "agent runs will return canned output and perform NO work. Never use "
            "this where a run's result is trusted."
        )
        return _scripted_fallback_runner()

    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_NO_PROVIDER_DETAIL)


@lru_cache(maxsize=1)
def _agent_runner_singleton() -> AgentRunner:
    return _default_runner()


def get_agent_runner() -> AgentRunner:
    """Return the process-wide agent runner (override in tests via DI)."""
    return _agent_runner_singleton()


def get_agent_store(session: SessionDep) -> AgentRunStore:
    """Return a request-scoped database store (override in tests via DI)."""
    return AgentRunStore(session)


@lru_cache(maxsize=1)
def _celery_app() -> Any:
    from celery import Celery

    url = os.environ.get("FORGE_REDIS_URL", "redis://localhost:6379/0")
    return Celery("forge-api-enqueue", broker=url, backend=url)


def enqueue_agent_run(objective: AgentObjective) -> None:
    _celery_app().send_task(
        "forge.agent.run",
        args=[objective.model_dump(mode="json")],
        kwargs={"idempotency_key": objective.context["agent_run_id"]},
        queue="forge",
    )


def get_agent_enqueue() -> Callable[[AgentObjective], None]:
    return enqueue_agent_run


RunnerDep = Annotated[AgentRunner, Depends(get_agent_runner)]
StoreDep = Annotated[AgentRunStore, Depends(get_agent_store)]
EnqueueDep = Annotated[Callable[[AgentObjective], None], Depends(get_agent_enqueue)]


# --------------------------------------------------------------------------- #
# Per-role model client factory (Time-Travel Runs — counterfactual fork)       #
# --------------------------------------------------------------------------- #

#: Turn a concrete model string into a :class:`~forge_contracts.ModelClient` — the
#: same per-role seam the coordinator uses (``CoordinatorDeps.model_client_factory``)
#: so a fork can re-run against a *different* model.
ForkModelFactory = Callable[[str], ModelClient]


def _default_fork_model_factory(model: str) -> ModelClient:
    """Build a client bound to ``model`` from the BYOK env config, else offline.

    Reuses the HARD-02 ``ModelClientConfig`` -> ``build_model_client`` seam,
    swapping in the requested ``model``. When no provider is configured (or its
    SDK is absent) it degrades to the offline deterministic
    :class:`ScriptedModelClient` so the fork endpoint still returns a
    counterfactual result without any live provider call (hermetic tests / no
    BYOK creds).
    """
    config = ModelClientConfig.from_env()
    if config is not None:
        try:
            return build_model_client(
                dataclasses.replace(config, model=model), redactor=redact_text
            )
        except ModelClientError:
            # The parent class: the SDK extra may be missing
            # (`ModelClientUnavailable`) or the configured provider may be
            # unbuildable (e.g. a blank bridge token). A fork is a
            # counterfactual and stays available offline either way.
            pass
    # Same rule as a live run: never assert confidence in a canned answer. A
    # fork is a counterfactual, so it stays available offline — but it says so.
    return ScriptedModelClient(
        responses=[],
        default=finish_response(
            "Offline fork: no model was called, so this counterfactual reflects no "
            "reasoning about the alternative.",
        ),
    )


def get_fork_model_factory() -> ForkModelFactory:
    """Return the fork model-client factory (override in tests via DI)."""
    return _default_fork_model_factory


ForkModelFactoryDep = Annotated[ForkModelFactory, Depends(get_fork_model_factory)]


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


def _requested_skill_profiles(objective: AgentObjective) -> list[str]:
    """The distinct skill-profile names this run requests (objective + targets)."""
    names: dict[str, None] = {}
    if objective.skill_profile is not None:
        names.setdefault(objective.skill_profile.name, None)
    for target in objective.repo_targets:
        if target.skill_profile:
            names.setdefault(target.skill_profile, None)
    return list(names)


def _enforce_skill_profiles(objective: AgentObjective) -> None:
    """Hard-enforce ``policy.skill_profiles.allowed`` before a run is admitted.

    The policy is loaded from ``context['repo_root']`` when supplied; a run that
    requests a profile the repo policy does not allow is rejected with HTTP 422
    (fail-closed governance). No resolvable policy means no restriction to apply.
    """
    repo_root = objective.context.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        return
    try:
        policy = load_policy(repo_root)
    except (FileNotFoundError, ValueError, ValidationError):
        return
    names = _requested_skill_profiles(objective)
    requested: list[str | None] = list(names) if names else [None]
    for name in requested:
        try:
            enforce_skill_profile_allowed(policy, name)
        except SkillProfileNotAllowedError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc


class CreateRunRequest(AgentObjective):
    """``POST /agent/runs`` body: an objective, or a tracker reference.

    With ``tracker_ref`` (``provider:key``, e.g.
    ``github_issues:acme/widgets#42``) the objective is built from the
    re-fetched tracker item instead and the caller's ``objective`` text is not
    required (spec M M3); without it, today's contract is unchanged.
    """

    objective: str = ""
    tracker_ref: str | None = None


def get_tracker_adapter_factory() -> Callable[[Any], Any]:
    """Resolve a PM adapter for a connection (lazy; override in tests).

    Construction is deferred until a ``tracker_ref`` actually needs resolving,
    so the plain run path never builds the PM service (vault/session wiring).
    """

    def _factory(connection: Any) -> Any:
        from forge_api.routers.pm import get_pm_service

        return get_pm_service().get_adapter_for_project(
            connection.workspace_id, connection.project_id, connection.provider.value
        )

    return _factory


TrackerAdapterFactoryDep = Annotated[Callable[[Any], Any], Depends(get_tracker_adapter_factory)]


def _objective_from_tracker_ref(
    principal: Principal,
    store: AgentRunStore,
    tracker_adapters: Callable[[Any], Any],
    body: CreateRunRequest,
) -> tuple[AgentObjective, dict]:
    assert body.tracker_ref is not None
    try:
        resolved = resolve_tracker_ref(store.session, principal.workspace_id, body.tracker_ref)
    except TrackerRefNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tracker ref not found"
        ) from exc
    adapter = tracker_adapters(resolved.connection)
    try:
        external = asyncio.run(adapter.fetch_external(resolved.external_id))
    except ExternalNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tracker ref not found"
        ) from exc

    dispatch_cfg = PMDispatchConfig()
    raw_dispatch = (resolved.connection.config or {}).get("dispatch")
    if raw_dispatch:
        try:
            dispatch_cfg = PMDispatchConfig.model_validate(raw_dispatch)
        except ValidationError:
            dispatch_cfg = PMDispatchConfig()
    repository = dispatch_cfg.repository
    if repository is None and body.primary_repo_target is not None:
        repository = body.primary_repo_target.repo
    if repository is None:
        key = resolved.connection.external_project_key
        repository = key if "/" in key else None
    return build_tracker_objective(
        external,
        connection_id=resolved.connection.id,
        repository=repository,
        actor=f"user:{principal.user_id}",
        task_id=resolved.task_id,
    )


@router.post("/runs", response_model=AgentRunResult, status_code=status.HTTP_202_ACCEPTED)
def run(
    principal: RunnerPrincipalDep,
    runner: RunnerDep,
    store: StoreDep,
    enqueue: EnqueueDep,
    tracker_adapters: TrackerAdapterFactoryDep,
    body: CreateRunRequest,
) -> AgentRunResult:
    """Admit a run only when a provider is available, then dispatch to the worker."""
    server_context: dict | None = None
    if body.tracker_ref:
        objective, server_context = _objective_from_tracker_ref(
            principal, store, tracker_adapters, body
        )
    else:
        if not body.objective:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="objective is required when tracker_ref is not given",
            )
        objective = AgentObjective.model_validate(body.model_dump(exclude={"tracker_ref"}))
        _enforce_skill_profiles(objective)
    try:
        return create_and_enqueue_run(
            store.session,
            workspace_id=principal.workspace_id,
            objective=objective,
            enqueue=enqueue,
            server_context=server_context,
        )
    except RepoNotConnected as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"repository {exc.args[0]!r} is not connected to this workspace",
        ) from exc
    except AgentEnqueueFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent run could not be enqueued",
        ) from exc


@router.get("/runs/{run_id}", response_model=AgentRunResult)
def get_run(store: StoreDep, principal: ReaderDep, run_id: uuid.UUID) -> AgentRunResult:
    """Fetch a recorded agent run result with its steps (own workspace only)."""
    result = store.get(run_id, workspace_id=principal.workspace_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no agent run {run_id}")
    return result


@router.post("/runs/{run_id}/replay", response_model=ReplayRunResponse)
def replay_run(
    run_id: uuid.UUID,
    body: ReplayRunRequest,
    principal: ReaderDep,
    session: SessionDep,
) -> ReplayRunResponse:
    """Time-Travel Runs: replay ``RunRecording`` ``run_id`` and diff it vs. the tape.

    Loads the workspace-scoped cassette (404 for a missing or foreign-tenant
    recording), reconstructs it via ``RunCassette.from_dict``, then re-runs
    ``body.objective`` through a fresh ``AgentRunner`` wired with the
    replay-by-substitution wrappers — no live model or tool is ever touched.
    Read-only (``Permission.READ``): a replay never mutates the recording or
    produces a new persisted run.
    """
    row = session.execute(
        select(RunRecording).where(
            RunRecording.id == run_id,
            RunRecording.workspace_id == principal.workspace_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no run recording {run_id}"
        )
    cassette = RunCassette.from_dict(row.cassette)
    outcome = replay_recording(cassette, body.objective)
    return ReplayRunResponse(
        run_recording_id=run_id,
        diverged=outcome.diverged,
        divergence=(
            ReplayDivergenceOut(
                boundary=outcome.divergence.boundary,
                index=outcome.divergence.index,
                name=outcome.divergence.name,
                expected=outcome.divergence.expected,
                actual=outcome.divergence.actual,
            )
            if outcome.divergence is not None
            else None
        ),
        steps=[
            ReplayCallDiffOut(
                boundary=step.boundary,
                index=step.index,
                name=step.name,
                matched=step.matched,
                recorded_digest=step.recorded_digest,
                replay_digest=step.replay_digest,
            )
            for step in outcome.steps
        ],
        result=outcome.result,
    )


@router.post("/runs/{run_id}/fork", response_model=ForkRunResponse)
def fork_run(
    run_id: uuid.UUID,
    body: ForkRunRequest,
    principal: RunnerPrincipalDep,
    session: SessionDep,
    model_factory: ForkModelFactoryDep,
) -> ForkRunResponse:
    """Time-Travel Runs: counterfactually fork ``RunRecording`` ``run_id``.

    Loads the workspace-scoped cassette (404 for a missing or foreign-tenant
    recording), reconstructs it, then replays the run up to ``body.fork_index``
    and lets it diverge: from the fork on, LLM completions run against a
    **different** model (``body.model``, built via the per-role
    ``model_client_factory`` seam, optionally with ``body.prompt_override``
    appended to the system prompt) and tool dispatches run live. Requires
    ``RUN_AGENT`` — a fork executes the agent (and, post-fork, real tools). If
    the *pre-fork* prefix no longer reproduces the tape the fork aborts and the
    divergence is reported (mirroring replay).
    """
    row = session.execute(
        select(RunRecording).where(
            RunRecording.id == run_id,
            RunRecording.workspace_id == principal.workspace_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no run recording {run_id}"
        )
    cassette = RunCassette.from_dict(row.cassette)

    new_client: ModelClient = model_factory(body.model)
    if body.prompt_override:
        new_client = PromptOverrideModelClient(new_client, body.prompt_override)

    outcome = fork_recording(
        cassette,
        body.objective,
        fork_index=body.fork_index,
        new_client=new_client,
    )
    return ForkRunResponse(
        run_recording_id=run_id,
        fork_index=outcome.fork_index,
        model=body.model,
        diverged=outcome.diverged,
        divergence=(
            ReplayDivergenceOut(
                boundary=outcome.divergence.boundary,
                index=outcome.divergence.index,
                name=outcome.divergence.name,
                expected=outcome.divergence.expected,
                actual=outcome.divergence.actual,
            )
            if outcome.divergence is not None
            else None
        ),
        result=outcome.result,
    )


__all__ = [
    "AgentRunStore",
    "get_agent_runner",
    "get_agent_store",
    "get_fork_model_factory",
    "router",
]
