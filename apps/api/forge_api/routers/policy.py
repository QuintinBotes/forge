"""Policy SDK router (Task 1.10 — policy-sdk; F29 conditional layer).

Serves the repo-policy surface over HTTP:

* ``GET  /policy``                 — load the effective ``.forge/policy.yaml``.
* ``POST /policy/evaluate``        — evaluate a :class:`~forge_contracts.ToolCall`
  against a policy (optionally with an F29 :class:`PolicyContext`).
* ``POST /policy/simulate``        — F29 dry-run: the composed decision + a
  per-rule trace (no persistence). Privileged (admin/member/agent-runner).
* ``POST /policy/test``            — F29 run a ``.forge/policy.tests.yaml`` suite.
* ``GET  /policy/rule-evaluations`` — F29 workspace-scoped conditional audit query.

Handlers delegate to a process-wide :class:`ConditionalPolicyEvaluator` /
:class:`PolicyService` (pure beyond reading the policy file + audit reads).
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ValidationError

from forge_api.auth.rbac import Permission
from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.policy import (
    PolicyRuleEvaluationOut,
    PolicyTestRequest,
    PolicyTestResponse,
    SimulateRequest,
    SimulationResult,
)
from forge_api.services.policy_service import PolicyService
from forge_contracts import Decision, Policy, ToolCall
from forge_policy import (
    ConditionalPolicyEvaluator,
    PolicyContext,
    resolve_policy_path,
    suite_path_for,
)
from forge_policy.tests_runner import load_test_suite

router = APIRouter(
    prefix="/policy",
    tags=["policy"],
    dependencies=[Depends(get_current_principal)],
)

# Loading/evaluating a policy is read-only; simulation/testing reveal decisions
# and are privileged (a read-only ``viewer`` is denied — F29 AC16).
ReadGate = Depends(require_permission(Permission.READ))
SimulateDep = Annotated[Principal, Depends(require_permission(Permission.RUN_AGENT))]
TestDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]


# --------------------------------------------------------------------------- #
# Dependencies (overridable for tests)                                        #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _policy_evaluator_singleton() -> ConditionalPolicyEvaluator:
    return ConditionalPolicyEvaluator()


def get_policy_evaluator() -> ConditionalPolicyEvaluator:
    """Return the process-wide conditional policy evaluator (override in tests)."""
    return _policy_evaluator_singleton()


@lru_cache(maxsize=1)
def _policy_service_singleton() -> PolicyService:
    return PolicyService(evaluator=_policy_evaluator_singleton())


def get_policy_service() -> PolicyService:
    """Return the process-wide policy service (override in tests via DI)."""
    return _policy_service_singleton()


EvaluatorDep = Annotated[ConditionalPolicyEvaluator, Depends(get_policy_evaluator)]
ServiceDep = Annotated[PolicyService, Depends(get_policy_service)]


# --------------------------------------------------------------------------- #
# Request bodies                                                              #
# --------------------------------------------------------------------------- #


class EvaluateRequest(BaseModel):
    """Body for ``POST /policy/evaluate``."""

    action: ToolCall
    policy: Policy | None = None
    repo_root: str | None = None
    context: PolicyContext | None = None


def _load_policy(evaluator: ConditionalPolicyEvaluator, repo_root: str) -> Policy:
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


def _resolve_policy(
    evaluator: ConditionalPolicyEvaluator, policy: Policy | None, repo_root: str | None
) -> Policy:
    if policy is not None:
        return policy
    if not repo_root:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="one of 'policy' or 'repo_root' is required",
        )
    return _load_policy(evaluator, repo_root)


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
    """Evaluate a tool call against the supplied or loaded repo policy.

    When an F29 ``context`` is supplied the conditional layer composes on top of
    the flat F04 decision; otherwise this is the flat F04 decision (unchanged).
    """
    policy = _resolve_policy(evaluator, request.policy, request.repo_root)
    context = request.context or PolicyContext.empty()
    return evaluator.evaluate_in_context(request.action, policy, context)


@router.post("/simulate", response_model=SimulationResult)
def simulate(
    evaluator: EvaluatorDep,
    service: ServiceDep,
    request: SimulateRequest,
    _principal: SimulateDep,
) -> SimulationResult:
    """Dry-run a decision with a full per-rule trace (no persistence)."""
    policy = _resolve_policy(evaluator, request.policy, request.repo_root)
    return service.simulate(request.action, policy, request.context)


@router.post("/test", response_model=PolicyTestResponse)
def test(
    evaluator: EvaluatorDep,
    service: ServiceDep,
    request: PolicyTestRequest,
    _principal: TestDep,
) -> PolicyTestResponse:
    """Run a ``.forge/policy.tests.yaml`` assertion suite against the policy."""
    policy = _resolve_policy(evaluator, request.policy, request.repo_root)
    suite = request.suite
    if suite is None:
        if not request.repo_root:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="one of 'suite' or 'repo_root' is required",
            )
        suite_path = suite_path_for(resolve_policy_path(request.repo_root))
        if not suite_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no policy test suite found at {suite_path}",
            )
        try:
            suite = load_test_suite(suite_path)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
    report = service.run_tests(policy, suite)
    return PolicyTestResponse.model_validate(report.model_dump())


@router.get("/rule-evaluations", response_model=list[PolicyRuleEvaluationOut])
def rule_evaluations(
    service: ServiceDep,
    session: DbSession,
    principal: ReaderDep,
    agent_run_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[PolicyRuleEvaluationOut]:
    """List conditional-decision audit rows for the caller's workspace."""
    rows = service.list_rule_evaluations(
        session,
        workspace_id=principal.workspace_id,
        agent_run_id=agent_run_id,
        limit=limit,
    )
    return [
        PolicyRuleEvaluationOut(
            id=str(row.id),
            action=row.action,
            base_effect=row.base_effect,
            final_effect=row.final_effect,
            requires_approval=row.requires_approval,
            severity=row.severity,
            matched_rule_ids=list(row.matched_rule_ids),
            context_redacted=dict(row.context_redacted),
            agent_run_id=str(row.agent_run_id) if row.agent_run_id else None,
            step_id=str(row.step_id) if row.step_id else None,
            evaluated_at=row.evaluated_at,
        )
        for row in rows
    ]


__all__ = ["EvaluateRequest", "get_policy_evaluator", "get_policy_service", "router"]
