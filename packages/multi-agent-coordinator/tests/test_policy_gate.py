"""Spawn policy gate enforcement (AC 5, 6, 7 resolution, 18)."""

from __future__ import annotations

from forge_contracts import (
    CoordinationPattern,
    SubAgentAssignment,
    SubAgentPolicy,
    SubAgentRole,
    SubagentRules,
    SupervisionPlan,
)
from forge_coordinator import CoordinatorSettings, evaluate_gate


def _plan(*roles_optional: tuple[SubAgentRole, bool]) -> SupervisionPlan:
    assignments = [
        SubAgentAssignment(
            id=f"sa-{role.value}-{i}",
            role=role,
            objective="x",
            ordinal=i,
            optional=optional,
        )
        for i, (role, optional) in enumerate(roles_optional)
    ]
    return SupervisionPlan(pattern=CoordinationPattern.SEQUENTIAL_PIPELINE, assignments=assignments)


def _rules(*, allow: bool = True, roles=("implementer",), mp: int = 2) -> SubagentRules:
    return SubagentRules(allow_subagents=allow, allowed_roles=list(roles), max_parallel=mp)


def test_feature_flag_off_blocks() -> None:
    gate = evaluate_gate(
        plan=_plan((SubAgentRole.IMPLEMENTER, False)),
        subagent_rules=_rules(mp=2),
        task_subagent_policy=SubAgentPolicy(allowed=True, max_parallel=2),
        settings=CoordinatorSettings(enabled=False),
    )
    assert not gate.ok
    assert gate.reason == "multi_agent_disabled"


def test_subagents_disallowed() -> None:
    gate = evaluate_gate(
        plan=_plan((SubAgentRole.IMPLEMENTER, False)),
        subagent_rules=_rules(allow=False, roles=(), mp=0),
        task_subagent_policy=SubAgentPolicy(allowed=False, max_parallel=0),
        settings=CoordinatorSettings(enabled=True),
    )
    assert not gate.ok
    assert gate.reason == "subagents_not_permitted"


def test_max_parallel_zero_not_permitted() -> None:
    gate = evaluate_gate(
        plan=_plan((SubAgentRole.IMPLEMENTER, False)),
        subagent_rules=_rules(mp=0),
        task_subagent_policy=SubAgentPolicy(allowed=True, max_parallel=0),
        settings=CoordinatorSettings(enabled=True),
    )
    assert not gate.ok
    assert gate.reason == "subagents_not_permitted"


def test_optional_role_disallowed_is_skipped() -> None:
    gate = evaluate_gate(
        plan=_plan((SubAgentRole.IMPLEMENTER, False), (SubAgentRole.SECURITY, True)),
        subagent_rules=_rules(mp=2),
        task_subagent_policy=SubAgentPolicy(allowed=True, max_parallel=2),
        settings=CoordinatorSettings(enabled=True),
    )
    assert gate.ok
    assert gate.skipped == {"sa-security-1"}


def test_required_role_disallowed_blocks() -> None:
    gate = evaluate_gate(
        plan=_plan((SubAgentRole.IMPLEMENTER, False), (SubAgentRole.SECURITY, False)),
        subagent_rules=_rules(mp=2),
        task_subagent_policy=SubAgentPolicy(allowed=True, max_parallel=2),
        settings=CoordinatorSettings(enabled=True),
    )
    assert not gate.ok
    assert gate.reason == "role_not_allowed:security"
    assert gate.blocked is True


def test_max_parallel_resolution_and_cap() -> None:
    gate = evaluate_gate(
        plan=_plan((SubAgentRole.IMPLEMENTER, False)),
        subagent_rules=_rules(mp=8),
        task_subagent_policy=SubAgentPolicy(allowed=True, max_parallel=3),
        settings=CoordinatorSettings(enabled=True, max_parallel_cap=2),
    )
    assert gate.ok
    assert gate.max_parallel == 2  # min(8, 3, cap=2)
