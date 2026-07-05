"""Deterministic, table-driven coordination-pattern selection (F27 §4).

``DefaultPatternSelector.select`` is a **pure** function over typed inputs: the
same inputs always produce the same :class:`SupervisionPlan`, and **no model
client is ever constructed** (the Supervisor routes by explicit policy, never LLM
judgement — Multi-Agent Rule).

Selection precedence (first match wins):

1. explicit ``objective.context["coordination_pattern"]`` hint -> that pattern
   verbatim (the only path to ``DYNAMIC_HANDOFF`` in V3).
2. ``directives.review_required`` and {implementer, reviewer} allowed
   -> ``MAKER_CHECKER``.
3. ``task_kind == "feature"`` and the full pipeline role set allowed
   -> ``SEQUENTIAL_PIPELINE``.
4. decomposable into >1 independent unit and ``max_parallel >= 2``
   -> ``FAN_OUT_FAN_IN``.
5. otherwise -> ``ORCHESTRATOR_WORKER`` (single implementer).

Resolved per-role ``allowed_actions`` = ``ROLE_TOOLS[role]`` ∩
``task.allowed_actions`` ∩ (skill allowlist if non-empty). It is never widened.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

from forge_contracts import (
    ROLE_TOOLS,
    AcceptanceCriterion,
    AgentObjective,
    CoordinationPattern,
    Policy,
    SubAgentAssignment,
    SubAgentPolicy,
    SubAgentRole,
    SubagentRules,
    SupervisionPlan,
)
from forge_skill.directives import SkillDirectives

__all__ = [
    "DefaultPatternSelector",
    "PatternSelector",
    "resolve_allowed_actions",
    "resolve_max_parallel",
]

_PIPELINE_ROLES = (
    SubAgentRole.RESEARCHER,
    SubAgentRole.PLANNER,
    SubAgentRole.IMPLEMENTER,
    SubAgentRole.TESTER,
    SubAgentRole.REVIEWER,
)


@runtime_checkable
class PatternSelector(Protocol):
    """Deterministic pattern selector (no LLM)."""

    def select(
        self,
        *,
        objective: AgentObjective,
        policy: Policy | None,
        subagent_rules: SubagentRules,
        task_subagent_policy: SubAgentPolicy,
        directives: SkillDirectives,
    ) -> SupervisionPlan: ...


def resolve_allowed_actions(
    role: SubAgentRole,
    task_allowed: Sequence[str],
    skill_allowed: frozenset[str],
) -> list[str]:
    """Resolve a role's tool set: ``ROLE_TOOLS[role]`` ∩ task ∩ skill (no widening).

    An empty ``task_allowed`` means the task imposes no extra restriction (the
    role tools stand); an empty ``skill_allowed`` means the skill imposes none.
    """
    resolved = set(ROLE_TOOLS[role])
    if task_allowed:
        resolved &= set(task_allowed)
    if skill_allowed:
        resolved &= set(skill_allowed)
    # Deterministic ordering for reproducible plans.
    return sorted(resolved)


def resolve_max_parallel(rules: SubagentRules, task: SubAgentPolicy) -> int:
    """Resolve ``max_parallel`` = min of the positive bounds, default 1."""
    bounds = [v for v in (rules.max_parallel, task.max_parallel) if v and v > 0]
    return min(bounds) if bounds else 1


class DefaultPatternSelector:
    """Pure, table-driven selector. Implements :class:`PatternSelector`."""

    def select(
        self,
        *,
        objective: AgentObjective,
        policy: Policy | None,
        subagent_rules: SubagentRules,
        task_subagent_policy: SubAgentPolicy,
        directives: SkillDirectives,
    ) -> SupervisionPlan:
        allowed_roles = {r.lower() for r in subagent_rules.allowed_roles}
        task_allowed = list(objective.allowed_actions)
        skill_allowed = directives.allowed_actions
        resolved_parallel = resolve_max_parallel(subagent_rules, task_subagent_policy)

        pattern = self._select_pattern(
            objective=objective,
            allowed_roles=allowed_roles,
            directives=directives,
            resolved_parallel=resolved_parallel,
        )

        assignments = self._build_assignments(
            pattern=pattern,
            objective=objective,
            task_allowed=task_allowed,
            skill_allowed=skill_allowed,
        )

        merge_strategy: Literal["sequential_integration", "fan_in_merge", "read_only"]
        if pattern is CoordinationPattern.FAN_OUT_FAN_IN:
            merge_strategy = "fan_in_merge"
            max_parallel = resolved_parallel
        else:
            merge_strategy = "sequential_integration"
            max_parallel = 1

        return SupervisionPlan(
            pattern=pattern,
            assignments=assignments,
            max_parallel=max_parallel,
            review_loop_budget=1,
            merge_strategy=merge_strategy,
        )

    # ------------------------------------------------------------------ #
    def _select_pattern(
        self,
        *,
        objective: AgentObjective,
        allowed_roles: set[str],
        directives: SkillDirectives,
        resolved_parallel: int,
    ) -> CoordinationPattern:
        hint = objective.context.get("coordination_pattern")
        if isinstance(hint, str):
            try:
                return CoordinationPattern(hint)
            except ValueError:
                pass

        has = allowed_roles.__contains__
        maker_roles = has("implementer") and has("reviewer")
        if directives.review_required and maker_roles:
            return CoordinationPattern.MAKER_CHECKER

        task_kind = str(objective.context.get("task_kind", "")).lower()
        pipeline_ok = all(r.value in allowed_roles for r in _PIPELINE_ROLES)
        if task_kind == "feature" and pipeline_ok:
            return CoordinationPattern.SEQUENTIAL_PIPELINE

        units = objective.context.get("fan_out_units")
        if (
            isinstance(units, list)
            and len(units) > 1
            and resolved_parallel >= 2
            and has("implementer")
        ):
            return CoordinationPattern.FAN_OUT_FAN_IN

        return CoordinationPattern.ORCHESTRATOR_WORKER

    def _build_assignments(
        self,
        *,
        pattern: CoordinationPattern,
        objective: AgentObjective,
        task_allowed: list[str],
        skill_allowed: frozenset[str],
    ) -> list[SubAgentAssignment]:
        criteria = list(objective.acceptance_criteria)

        def make(
            role: SubAgentRole,
            *,
            idx: int,
            objective_text: str,
            ordinal: int,
            depends_on: list[str] | None = None,
            optional: bool = False,
            acceptance: list[AcceptanceCriterion] | None = None,
        ) -> SubAgentAssignment:
            return SubAgentAssignment(
                id=f"sa-{role.value}-{idx}",
                role=role,
                objective=objective_text,
                acceptance_criteria=acceptance if acceptance is not None else [],
                allowed_actions=resolve_allowed_actions(role, task_allowed, skill_allowed),
                depends_on=depends_on or [],
                ordinal=ordinal,
                optional=optional,
            )

        base = objective.objective

        if pattern is CoordinationPattern.ORCHESTRATOR_WORKER:
            return [
                make(
                    SubAgentRole.IMPLEMENTER,
                    idx=1,
                    objective_text=base,
                    ordinal=0,
                    acceptance=criteria,
                )
            ]

        if pattern is CoordinationPattern.MAKER_CHECKER:
            impl = make(
                SubAgentRole.IMPLEMENTER,
                idx=1,
                objective_text=base,
                ordinal=0,
                acceptance=criteria,
            )
            reviewer = make(
                SubAgentRole.REVIEWER,
                idx=1,
                objective_text=f"Review the implementation for: {base}",
                ordinal=1,
                depends_on=[impl.id],
                acceptance=criteria,
            )
            return [impl, reviewer]

        if pattern is CoordinationPattern.SEQUENTIAL_PIPELINE:
            researcher = make(
                SubAgentRole.RESEARCHER, idx=1, objective_text=f"Research: {base}", ordinal=0
            )
            planner = make(
                SubAgentRole.PLANNER,
                idx=1,
                objective_text=f"Plan: {base}",
                ordinal=1,
                depends_on=[researcher.id],
            )
            impl = make(
                SubAgentRole.IMPLEMENTER,
                idx=1,
                objective_text=base,
                ordinal=2,
                depends_on=[planner.id],
                acceptance=criteria,
            )
            tester = make(
                SubAgentRole.TESTER,
                idx=1,
                objective_text=f"Write tests for: {base}",
                ordinal=3,
                depends_on=[impl.id],
            )
            reviewer = make(
                SubAgentRole.REVIEWER,
                idx=1,
                objective_text=f"Review: {base}",
                ordinal=4,
                depends_on=[impl.id, tester.id],
                acceptance=criteria,
            )
            return [researcher, planner, impl, tester, reviewer]

        if pattern is CoordinationPattern.FAN_OUT_FAN_IN:
            units = objective.context.get("fan_out_units") or []
            out: list[SubAgentAssignment] = []
            for i, unit in enumerate(units, start=1):
                text = unit.get("objective", base) if isinstance(unit, dict) else str(unit)
                out.append(
                    make(
                        SubAgentRole.IMPLEMENTER,
                        idx=i,
                        objective_text=text,
                        ordinal=i - 1,
                    )
                )
            return out or [make(SubAgentRole.IMPLEMENTER, idx=1, objective_text=base, ordinal=0)]

        # DYNAMIC_HANDOFF — explicit deterministic route from the task hint.
        plan_steps = objective.context.get("handoff_plan")
        if isinstance(plan_steps, list) and plan_steps:
            out2: list[SubAgentAssignment] = []
            id_by_index: dict[int, str] = {}
            for i, step in enumerate(plan_steps):
                role = SubAgentRole(str(step["role"]))
                deps = [id_by_index[d] for d in step.get("depends_on", []) if d in id_by_index]
                a = make(
                    role,
                    idx=i + 1,
                    objective_text=str(step.get("objective", base)),
                    ordinal=i,
                    depends_on=deps,
                    acceptance=criteria if role is SubAgentRole.IMPLEMENTER else [],
                )
                id_by_index[i] = a.id
                out2.append(a)
            return out2
        return [make(SubAgentRole.IMPLEMENTER, idx=1, objective_text=base, ordinal=0)]


# Structural-conformance guard: fail import if the class drifts from the Protocol.
_: PatternSelector = DefaultPatternSelector()
