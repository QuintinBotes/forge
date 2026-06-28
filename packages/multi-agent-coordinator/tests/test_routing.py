"""Pure routing predicates over crafted supervision state (AC 9, 12, 13, 16)."""

from __future__ import annotations

import uuid

from forge_contracts import (
    AgentObjective,
    MergeConflict,
    MergeResult,
    SubAgentAssignment,
    SubAgentPolicy,
    SubAgentRole,
    SubagentRules,
)
from forge_coordinator.routing import (
    router_after_dispatch,
    router_after_gate,
    router_after_merge,
)
from forge_coordinator.state import SupervisionState
from forge_skill.directives import SkillDirectives


def _state(**kw) -> SupervisionState:
    return SupervisionState(
        objective=AgentObjective(objective="x"),
        parent_agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        subagent_rules=SubagentRules(allow_subagents=True),
        task_subagent_policy=SubAgentPolicy(allowed=True),
        directives=SkillDirectives(name="c"),
        threshold=0.72,
        **kw,
    )


def _assignment(aid: str, role: SubAgentRole, deps: list[str] | None = None, ordinal: int = 0):
    return SubAgentAssignment(
        id=aid, role=role, objective="x", depends_on=deps or [], ordinal=ordinal
    )


def test_gate_routes_to_finalize_on_conflict() -> None:
    s = _state(policy_conflict="subagents_not_permitted", needs_human=True)
    assert router_after_gate(s) == "finalize"


def test_gate_routes_to_dispatch_when_ready() -> None:
    s = _state()
    s.assignments = {"a": _assignment("a", SubAgentRole.IMPLEMENTER)}
    s.statuses = {"a": "pending"}
    assert router_after_gate(s) == "dispatch"


def test_dispatch_routes_to_dispatch_when_more_ready() -> None:
    s = _state()
    s.assignments = {
        "i": _assignment("i", SubAgentRole.IMPLEMENTER),
        "r": _assignment("r", SubAgentRole.REVIEWER, deps=["i"], ordinal=1),
    }
    s.statuses = {"i": "succeeded", "r": "pending"}
    assert router_after_dispatch(s) == "dispatch"


def test_dispatch_routes_to_merge_when_all_done() -> None:
    s = _state()
    s.assignments = {"i": _assignment("i", SubAgentRole.IMPLEMENTER)}
    s.statuses = {"i": "succeeded"}
    assert router_after_dispatch(s) == "merge"


def test_dispatch_routes_to_finalize_on_interrupt() -> None:
    s = _state(needs_human=True, needs_human_reason="subagent_awaiting_input:i")
    s.assignments = {"i": _assignment("i", SubAgentRole.IMPLEMENTER)}
    s.statuses = {"i": "awaiting_input"}
    assert router_after_dispatch(s) == "finalize"


def test_merge_routes_to_finalize_on_conflict() -> None:
    s = _state()
    s.merge_result = MergeResult(
        integration_branch="forge/int",
        conflicts=[MergeConflict(assignment_id="i", path="x.py", detail="conflict")],
    )
    assert router_after_merge(s) == "finalize"


def test_merge_routes_to_validate_when_clean() -> None:
    s = _state()
    s.merge_result = MergeResult(integration_branch="forge/int", conflicts=[])
    assert router_after_merge(s) == "validate"


def test_dynamic_handoff_dependency_gating_is_pure() -> None:
    # Deterministic re-route: the next role becomes ready only when its dep is done.
    s = _state()
    s.assignments = {
        "a": _assignment("a", SubAgentRole.IMPLEMENTER),
        "b": _assignment("b", SubAgentRole.REVIEWER, deps=["a"], ordinal=1),
    }
    s.statuses = {"a": "running", "b": "pending"}
    # 'b' not ready until 'a' succeeds -> no ready assignment yet -> merge.
    assert router_after_dispatch(s) == "merge"
    s.statuses["a"] = "succeeded"
    assert router_after_dispatch(s) == "dispatch"
