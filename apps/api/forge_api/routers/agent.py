"""Agent runtime router (Task 1.9 — agent-runtime; wired in Phase 2 Task 2.1).

Serves the single-agent loop (plan -> act -> observe) over HTTP:

* ``POST /agent/runs``           — run an :class:`~forge_contracts.AgentObjective`
  and return the :class:`~forge_contracts.AgentRunResult` (with its step trace).
* ``GET  /agent/runs/{run_id}``  — fetch a previously-recorded run result.

Handlers delegate to a process-wide :class:`~forge_agent.AgentRunner`. The default
runner is driven by an offline-safe scripted model client (deterministic, no live
provider calls) so the runtime executes end-to-end without network; a real BYOK
:class:`~forge_contracts.ModelClient` is swapped in behind the same dependency via
``app.dependency_overrides`` / config. Completed runs are kept in an in-memory
store so ``GET /agent/runs/{run_id}`` can return them.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from forge_agent import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_api.auth.rbac import Permission
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_contracts import AgentObjective, AgentRunResult
from forge_policy import (
    SkillProfileNotAllowedError,
    enforce_skill_profile_allowed,
    load_policy,
)

router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(get_current_principal)],
)

# Authorization: starting a run requires RUN_AGENT (member / agent-runner /
# admin); a read-only viewer is denied. Reading a run requires READ.
RunnerPrincipalDep = Annotated[Principal, Depends(require_permission(Permission.RUN_AGENT))]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]


# --------------------------------------------------------------------------- #
# In-memory run store                                                          #
# --------------------------------------------------------------------------- #


class AgentRunStore:
    """A tiny in-memory store of completed agent run results (keyed by run_id).

    Each result is tagged with the owning ``workspace_id`` so one tenant cannot
    read another tenant's run (``AgentRunResult`` is a frozen contract with no
    ``workspace_id`` field, so ownership is tracked alongside the results).
    """

    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, AgentRunResult] = {}
        self._owner: dict[uuid.UUID, uuid.UUID] = {}

    def put(self, result: AgentRunResult, *, workspace_id: uuid.UUID) -> AgentRunResult:
        if result.run_id is None:
            result.run_id = uuid.uuid4()
        self._runs[result.run_id] = result
        self._owner[result.run_id] = workspace_id
        return result

    def get(self, run_id: uuid.UUID, *, workspace_id: uuid.UUID) -> AgentRunResult | None:
        if self._owner.get(run_id) != workspace_id:
            return None
        return self._runs.get(run_id)


# --------------------------------------------------------------------------- #
# Runner + store dependencies (overridable for tests / BYOK swap)             #
# --------------------------------------------------------------------------- #


def _default_runner() -> AgentRunner:
    # Offline-safe deterministic model: every objective finishes cleanly without
    # any live provider call. A real ModelClient is injected in production.
    model = ScriptedModelClient(
        responses=[],
        default=finish_response(
            "Objective acknowledged; no offline model actions were required.",
            confidence=0.9,
        ),
    )
    return AgentRunner(model)


@lru_cache(maxsize=1)
def _agent_runner_singleton() -> AgentRunner:
    return _default_runner()


@lru_cache(maxsize=1)
def _agent_store_singleton() -> AgentRunStore:
    return AgentRunStore()


def get_agent_runner() -> AgentRunner:
    """Return the process-wide agent runner (override in tests via DI)."""
    return _agent_runner_singleton()


def get_agent_store() -> AgentRunStore:
    """Return the process-wide agent run store (override in tests via DI)."""
    return _agent_store_singleton()


RunnerDep = Annotated[AgentRunner, Depends(get_agent_runner)]
StoreDep = Annotated[AgentRunStore, Depends(get_agent_store)]


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


@router.post("/runs", response_model=AgentRunResult, status_code=status.HTTP_201_CREATED)
def run(
    runner: RunnerDep,
    store: StoreDep,
    principal: RunnerPrincipalDep,
    objective: AgentObjective,
) -> AgentRunResult:
    """Run an agent objective (plan -> act -> observe) and record the result."""
    _enforce_skill_profiles(objective)
    result = runner.run(objective)
    return store.put(result, workspace_id=principal.workspace_id)


@router.get("/runs/{run_id}", response_model=AgentRunResult)
def get_run(store: StoreDep, principal: ReaderDep, run_id: uuid.UUID) -> AgentRunResult:
    """Fetch a recorded agent run result with its steps (own workspace only)."""
    result = store.get(run_id, workspace_id=principal.workspace_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no agent run {run_id}")
    return result


__all__ = ["AgentRunStore", "get_agent_runner", "get_agent_store", "router"]
