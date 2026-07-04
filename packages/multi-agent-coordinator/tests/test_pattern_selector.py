"""Deterministic pattern selection (AC 2, 4, 6 selector parts)."""

from __future__ import annotations

import dataclasses

from forge_contracts import (
    ROLE_TOOLS,
    AgentObjective,
    CoordinationPattern,
    SubAgentPolicy,
    SubagentRules,
)
from forge_coordinator import CoordinatorDeps, DefaultPatternSelector
from forge_skill.directives import SkillDirectives


def _select(objective: AgentObjective, rules: SubagentRules, directives: SkillDirectives):
    return DefaultPatternSelector().select(
        objective=objective,
        policy=None,
        subagent_rules=rules,
        task_subagent_policy=SubAgentPolicy(allowed=True, max_parallel=2),
        directives=directives,
    )


def test_maker_checker_on_review_required() -> None:
    rules = SubagentRules(
        allow_subagents=True, allowed_roles=["implementer", "reviewer"], max_parallel=2
    )
    plan = _select(
        AgentObjective(objective="x"), rules, SkillDirectives(name="c", review_required=True)
    )
    assert plan.pattern is CoordinationPattern.MAKER_CHECKER
    roles = [a.role.value for a in plan.assignments]
    assert roles == ["implementer", "reviewer"]


def test_sequential_pipeline_on_feature_full_roles() -> None:
    rules = SubagentRules(
        allow_subagents=True,
        allowed_roles=["researcher", "planner", "implementer", "tester", "reviewer"],
        max_parallel=2,
    )
    obj = AgentObjective(objective="x", context={"task_kind": "feature"})
    plan = _select(obj, rules, SkillDirectives(name="c"))
    assert plan.pattern is CoordinationPattern.SEQUENTIAL_PIPELINE
    assert [a.role.value for a in plan.assignments] == [
        "researcher",
        "planner",
        "implementer",
        "tester",
        "reviewer",
    ]


def test_fan_out_on_decomposable() -> None:
    rules = SubagentRules(allow_subagents=True, allowed_roles=["implementer"], max_parallel=2)
    obj = AgentObjective(
        objective="x",
        context={"fan_out_units": [{"objective": "a"}, {"objective": "b"}]},
    )
    plan = _select(obj, rules, SkillDirectives(name="c"))
    assert plan.pattern is CoordinationPattern.FAN_OUT_FAN_IN
    assert plan.merge_strategy == "fan_in_merge"
    assert len(plan.assignments) == 2


def test_orchestrator_worker_default() -> None:
    rules = SubagentRules(allow_subagents=True, allowed_roles=["implementer"], max_parallel=1)
    plan = _select(AgentObjective(objective="x"), rules, SkillDirectives(name="c"))
    assert plan.pattern is CoordinationPattern.ORCHESTRATOR_WORKER
    assert len(plan.assignments) == 1


def test_explicit_hint_selects_dynamic_handoff_verbatim() -> None:
    rules = SubagentRules(
        allow_subagents=True, allowed_roles=["implementer", "reviewer"], max_parallel=2
    )
    obj = AgentObjective(
        objective="x",
        context={
            "coordination_pattern": "dynamic_handoff",
            "handoff_plan": [
                {"role": "implementer", "objective": "build"},
                {"role": "reviewer", "objective": "review", "depends_on": [0]},
            ],
        },
    )
    plan = _select(obj, rules, SkillDirectives(name="c", review_required=True))
    assert plan.pattern is CoordinationPattern.DYNAMIC_HANDOFF
    assert plan.assignments[1].depends_on == [plan.assignments[0].id]


def test_selection_is_deterministic() -> None:
    rules = SubagentRules(
        allow_subagents=True, allowed_roles=["implementer", "reviewer"], max_parallel=2
    )
    obj = AgentObjective(objective="x")
    directives = SkillDirectives(name="c", review_required=True)
    a = _select(obj, rules, directives)
    b = _select(obj, rules, directives)
    assert a.model_dump() == b.model_dump()


def test_no_assignment_widens_role_tools() -> None:
    rules = SubagentRules(
        allow_subagents=True,
        allowed_roles=["researcher", "planner", "implementer", "tester", "reviewer"],
        max_parallel=2,
    )
    obj = AgentObjective(objective="x", context={"task_kind": "feature"})
    plan = _select(obj, rules, SkillDirectives(name="c"))
    for a in plan.assignments:
        assert set(a.allowed_actions) <= set(ROLE_TOOLS[a.role]), a.role


def test_deps_carry_no_model_factory() -> None:
    # AC 2: the supervisor's dependency set has no LLM/model factory at all.
    field_names = {f.name for f in dataclasses.fields(CoordinatorDeps)}
    assert "model_factory" not in field_names
    assert "model" not in field_names
    assert "agent_factory" in field_names
