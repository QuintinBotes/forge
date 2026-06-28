"""The spawn policy gate (F27 §3.3 ``policy_gate`` node).

Enforces the ``subagent_rules`` envelope that F04 §12 explicitly deferred to the
V3 coordinator: ``allow_subagents``, ``allowed_roles``, and ``max_parallel``
(intersected with the task policy and the deploy ``max_parallel_cap``). The
Supervisor cannot grant a role the policy forbids, and it never silently
downgrades or widens scope — a forbidden **required** role blocks the run; a
forbidden **optional** role is skipped and recorded.

Foundation note: F04's ``spawn_subagent`` ``Decision`` is encoded here as
``role ∈ subagent_rules.allowed_roles`` (the repo-policy authority for spawning);
an injected :class:`~forge_contracts.PolicyEvaluator` is consulted only when a
caller supplies an explicit spawn policy, so the default evaluator's deny-by-
default never spuriously blocks a policy-permitted role.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_contracts import SubAgentPolicy, SubagentRules, SupervisionPlan
from forge_coordinator.settings import CoordinatorSettings

__all__ = ["GateDecision", "evaluate_gate"]


@dataclass
class GateDecision:
    """The outcome of the policy gate."""

    ok: bool
    reason: str | None = None
    max_parallel: int = 0
    skipped: set[str] = field(default_factory=set)
    blocked: bool = False


def evaluate_gate(
    *,
    plan: SupervisionPlan,
    subagent_rules: SubagentRules,
    task_subagent_policy: SubAgentPolicy,
    settings: CoordinatorSettings,
) -> GateDecision:
    """Evaluate the spawn gate for ``plan`` and return a :class:`GateDecision`."""
    if not settings.enabled:
        return GateDecision(ok=False, reason="multi_agent_disabled")

    if not subagent_rules.allow_subagents:
        return GateDecision(ok=False, reason="subagents_not_permitted")

    effective = subagent_rules.max_parallel
    if task_subagent_policy.max_parallel and task_subagent_policy.max_parallel > 0:
        effective = min(effective, task_subagent_policy.max_parallel)
    effective = min(effective, settings.max_parallel_cap)
    if effective < 1:
        return GateDecision(ok=False, reason="subagents_not_permitted")

    allowed_roles = {r.strip().lower() for r in subagent_rules.allowed_roles}
    skipped: set[str] = set()
    blocked_role: str | None = None
    for assignment in plan.assignments:
        role = assignment.role.value
        if role in allowed_roles:
            continue
        if assignment.optional:
            skipped.add(assignment.id)
        else:
            blocked_role = blocked_role or role

    if blocked_role is not None:
        return GateDecision(
            ok=False,
            reason=f"role_not_allowed:{blocked_role}",
            max_parallel=effective,
            skipped=skipped,
            blocked=True,
        )

    return GateDecision(ok=True, max_parallel=effective, skipped=skipped)
