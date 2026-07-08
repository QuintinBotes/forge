"""Adaptive Orchestration execution planning (ao-policy).

Composes the three Adaptive Orchestration subsystems into a single deterministic
:class:`ExecutionPlan` for a task/spec — the answer to *how* to run the work:

* **ao-complexity** (:func:`forge_orchestration_policy.score_complexity`) sizes the
  task signals into ``{tier, strategy, score}``.
* **ao-config** (:func:`forge_orchestration_policy.resolve_effective_config`)
  resolves each role's ``{model_or_tier, effort}`` from the workspace/project
  overrides + hardcoded defaults.
* **ao-model-router** (:class:`forge_agent.providers.router.ModelRouter`) maps a
  seniority tier to a concrete BYOK provider model.

The plan carries a ``strategy`` (``single`` | ``swarm``) that gates whether the
coordinator fans a swarm out, a per-role ``{model, effort}`` the coordinator /
single-agent runtime applies, and a ``review_loop_budget`` that scales the
review/verify depth with complexity.

Two override rules keep humans in control:

* an explicit ``strategy_override`` always wins over the sized strategy;
* an explicit per-role override — a concrete model id, or a tier keyword a human
  pinned via :class:`~forge_contracts.orchestration_config.RoleConfigStore` — is
  respected verbatim and **never** escalated by task complexity. Only a *default*
  tier is escalated (never demoted) toward the task's sized tier, so complex work
  reaches for a stronger model while a human's explicit choice stands.

This module performs no I/O and calls no model: it is pure over its inputs (the
same signals + store + router always produce the same plan), so it is safe to
run inside the deterministic, LLM-free supervisor graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast
from uuid import UUID

from forge_orchestration_policy import (
    ComplexitySizing,
    SizingSignals,
    Strategy,
    Tier,
    score_complexity,
)
from forge_orchestration_policy.role_config import resolve_effective_config

from forge_agent.providers.config import ProviderName
from forge_agent.providers.router import ModelRouter
from forge_contracts.orchestration_config import (
    AgentRole,
    Effort,
    RoleConfigSource,
    RoleConfigStore,
)

__all__ = [
    "ExecutionPlan",
    "RoleExecution",
    "plan_execution",
    "plan_role_execution",
]

#: Ordered seniority ladder used to escalate (never demote) a default role tier
#: toward the task's sized tier.
_TIER_ORDER: dict[Tier, int] = {"junior": 0, "medior": 1, "senior": 2}
_TIER_BY_ORDER: dict[int, Tier] = {v: k for k, v in _TIER_ORDER.items()}
_TIERS: frozenset[str] = frozenset(_TIER_ORDER)

#: Base review/verify loop depth per sized tier. A swarm always gets at least two
#: loops (a fan-out earns a second review pass), so simple single-agent work stays
#: cheap while complex work verifies harder.
_LOOP_BUDGET_BY_TIER: dict[Tier, int] = {"junior": 1, "medior": 2, "senior": 3}


def _is_tier(value: str) -> bool:
    return value in _TIERS


def _escalate(base: Tier, task_tier: Tier) -> Tier:
    """Return the more-senior of ``base`` and ``task_tier`` (never a demotion)."""
    return _TIER_BY_ORDER[max(_TIER_ORDER[base], _TIER_ORDER[task_tier])]


def _loop_budget(sizing: ComplexitySizing) -> int:
    budget = _LOOP_BUDGET_BY_TIER[sizing.tier]
    if sizing.strategy == "swarm":
        budget = max(budget, 2)
    return budget


@dataclass(frozen=True)
class RoleExecution:
    """The resolved ``{model, effort}`` for one Adaptive Orchestration role."""

    role: AgentRole
    model: str
    effort: Effort
    #: The effective tier when the config named a tier keyword (after any
    #: complexity escalation); ``None`` when a human pinned a concrete model id.
    tier: Tier | None
    source: RoleConfigSource
    #: ``True`` when task complexity escalated a *default* tier above its baseline.
    escalated: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExecutionPlan:
    """How to run a task/spec: strategy + per-role model/effort + loop depth."""

    strategy: Strategy
    #: ``override`` when an explicit ``strategy_override`` was supplied, else
    #: ``complexity`` (the sized strategy stood).
    strategy_source: Literal["complexity", "override"]
    sizing: ComplexitySizing
    roles: Mapping[AgentRole, RoleExecution]
    review_loop_budget: int
    reasons: list[str] = field(default_factory=list)

    def for_role(self, role: AgentRole) -> RoleExecution:
        """The :class:`RoleExecution` for ``role`` (all five roles are always present)."""
        return self.roles[role]


def plan_role_execution(
    role: AgentRole,
    *,
    sizing: ComplexitySizing,
    store: RoleConfigStore,
    workspace_id: UUID,
    router: ModelRouter,
    project_id: UUID | None = None,
) -> RoleExecution:
    """Resolve one role's ``{model, effort}`` from its config + the task sizing.

    A *default* tier is escalated toward ``sizing.tier`` (never demoted); an
    explicit override — a workspace/project tier or a concrete model id — is used
    verbatim. Tier keywords are resolved to a concrete model through ``router``.
    """
    eff = resolve_effective_config(store, workspace_id, role, project_id=project_id)
    reasons = [f"config source={eff.source} model_or_tier={eff.model_or_tier}"]

    if _is_tier(eff.model_or_tier):
        base_tier = cast(Tier, eff.model_or_tier)
        if eff.source == "default":
            tier = _escalate(base_tier, sizing.tier)
            escalated = tier != base_tier
            if escalated:
                reasons.append(f"escalated default {base_tier}->{tier} (task tier={sizing.tier})")
        else:
            # Human-pinned tier: respected verbatim, never escalated.
            tier = base_tier
            escalated = False
        model = router.resolve(tier)
        reasons.append(f"tier={tier} -> model={model}")
        return RoleExecution(
            role=role,
            model=model,
            effort=eff.effort,
            tier=tier,
            source=eff.source,
            escalated=escalated,
            reasons=reasons,
        )

    # A concrete model id pinned by a human override: used exactly as given.
    reasons.append(f"pinned model={eff.model_or_tier} (verbatim, not escalated)")
    return RoleExecution(
        role=role,
        model=eff.model_or_tier,
        effort=eff.effort,
        tier=None,
        source=eff.source,
        escalated=False,
        reasons=reasons,
    )


def plan_execution(
    *,
    store: RoleConfigStore,
    workspace_id: UUID,
    signals: SizingSignals | None = None,
    sizing: ComplexitySizing | None = None,
    router: ModelRouter | None = None,
    provider: ProviderName | None = None,
    project_id: UUID | None = None,
    strategy_override: Strategy | None = None,
) -> ExecutionPlan:
    """Produce a deterministic :class:`ExecutionPlan` for a task/spec.

    Supply the task either as ``signals`` (scored here via
    :func:`~forge_orchestration_policy.score_complexity`) or as a pre-computed
    ``sizing``. Supply the model map either as a ready ``router`` (operator tier
    overrides) or a ``provider`` (the built-in default tier map). An explicit
    ``strategy_override`` wins over the sized strategy.
    """
    if sizing is None:
        if signals is None:
            raise ValueError("plan_execution requires either signals or sizing")
        sizing = score_complexity(signals)

    if router is None:
        if provider is None:
            raise ValueError("plan_execution requires either router or provider")
        router = ModelRouter(provider=provider)
    elif provider is not None and provider is not router.provider:
        raise ValueError(
            f"provider {provider.value!r} conflicts with router.provider {router.provider.value!r}"
        )

    if strategy_override is not None:
        strategy: Strategy = strategy_override
        strategy_source: Literal["complexity", "override"] = "override"
    else:
        strategy = sizing.strategy
        strategy_source = "complexity"

    roles = {
        role: plan_role_execution(
            role,
            sizing=sizing,
            store=store,
            workspace_id=workspace_id,
            router=router,
            project_id=project_id,
        )
        for role in AgentRole
    }

    review_loop_budget = _loop_budget(sizing)
    reasons = [
        f"sized tier={sizing.tier} strategy={sizing.strategy} score={sizing.score}",
        f"strategy={strategy} (source={strategy_source})",
        f"review_loop_budget={review_loop_budget}",
    ]

    return ExecutionPlan(
        strategy=strategy,
        strategy_source=strategy_source,
        sizing=sizing,
        roles=roles,
        review_loop_budget=review_loop_budget,
        reasons=reasons,
    )
