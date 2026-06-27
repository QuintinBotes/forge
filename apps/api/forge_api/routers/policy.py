"""Policy SDK router (Task 1.10 — policy-sdk; wired in Phase 2 Task 2.1).

Serves the repo-policy surface over HTTP:

* ``GET  /policy``          — load the effective ``.forge/policy.yaml`` for a repo
  (``repo_root`` query param: a repo directory or a direct path to a policy file).
* ``POST /policy/evaluate`` — evaluate a :class:`~forge_contracts.ToolCall`
  against a policy. The policy is taken from the request body if supplied, else
  loaded from ``repo_root``.

Handlers delegate to a process-wide :class:`~forge_policy.RepoPolicyEvaluator`
(pure: no I/O beyond reading the policy file, no network). Errors map to HTTP:
missing policy file -> 404; empty/invalid policy -> 422; a request that supplies
neither ``policy`` nor ``repo_root`` -> 422.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ValidationError

from forge_api.auth.rbac import Permission
from forge_api.deps import get_current_principal
from forge_api.routers._rbac import require_permission
from forge_contracts import Decision, Policy, ToolCall
from forge_policy import RepoPolicyEvaluator

router = APIRouter(
    prefix="/policy",
    tags=["policy"],
    dependencies=[Depends(get_current_principal)],
)

# Loading and evaluating a policy are read-only; any authenticated role with READ
# may call them, but unauthenticated callers are still rejected (401) upstream.
ReadGate = Depends(require_permission(Permission.READ))


# --------------------------------------------------------------------------- #
# Evaluator dependency (overridable for tests)                                 #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _policy_evaluator_singleton() -> RepoPolicyEvaluator:
    return RepoPolicyEvaluator()


def get_policy_evaluator() -> RepoPolicyEvaluator:
    """Return the process-wide policy evaluator (override in tests via DI)."""
    return _policy_evaluator_singleton()


EvaluatorDep = Annotated[RepoPolicyEvaluator, Depends(get_policy_evaluator)]


# --------------------------------------------------------------------------- #
# Request bodies                                                              #
# --------------------------------------------------------------------------- #


class EvaluateRequest(BaseModel):
    """Body for ``POST /policy/evaluate``."""

    action: ToolCall
    policy: Policy | None = None
    repo_root: str | None = None


def _load_policy(evaluator: RepoPolicyEvaluator, repo_root: str) -> Policy:
    try:
        return evaluator.load(repo_root)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.get("", response_model=Policy, dependencies=[ReadGate])
def load(
    evaluator: EvaluatorDep,
    repo_root: Annotated[str, Query(description="Repo directory or policy file path.")],
) -> Policy:
    """Load the effective repo policy from ``<repo_root>/.forge/policy.yaml``."""
    return _load_policy(evaluator, repo_root)


@router.post("/evaluate", response_model=Decision, dependencies=[ReadGate])
def evaluate(evaluator: EvaluatorDep, request: EvaluateRequest) -> Decision:
    """Evaluate a tool call against the supplied or loaded repo policy."""
    policy = request.policy
    if policy is None:
        if not request.repo_root:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="one of 'policy' or 'repo_root' is required",
            )
        policy = _load_policy(evaluator, request.repo_root)
    return evaluator.evaluate(request.action, policy)


__all__ = ["EvaluateRequest", "get_policy_evaluator", "router"]
