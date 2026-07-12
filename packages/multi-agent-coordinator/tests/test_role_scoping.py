"""Role tool scoping — no self-expansion (AC 4)."""

from __future__ import annotations

from forge_contracts import SubAgentPolicy, SubAgentRole, SubagentRules
from forge_coordinator import resolve_allowed_actions, resolve_max_parallel


def test_implementer_lacks_query_mcp() -> None:
    actions = resolve_allowed_actions(
        SubAgentRole.IMPLEMENTER, task_allowed=[], skill_allowed=frozenset()
    )
    assert "write_code" in actions
    assert "query_mcp" not in actions


def test_researcher_lacks_write_code_has_query_mcp() -> None:
    actions = resolve_allowed_actions(
        SubAgentRole.RESEARCHER, task_allowed=[], skill_allowed=frozenset()
    )
    assert "query_mcp" in actions
    assert "write_code" not in actions


def test_reviewer_is_read_only_plus_comment() -> None:
    actions = set(
        resolve_allowed_actions(SubAgentRole.REVIEWER, task_allowed=[], skill_allowed=frozenset())
    )
    assert actions == {"read_repo", "read_spec", "write_review_comment"}


def test_adversary_can_write_tests_but_not_product_code() -> None:
    actions = set(
        resolve_allowed_actions(SubAgentRole.ADVERSARY, task_allowed=[], skill_allowed=frozenset())
    )
    assert actions == {"read_repo", "run_tests", "write_test", "run_sast"}
    assert "write_code" not in actions
    assert "open_pr" not in actions


def test_task_allowlist_intersects_and_never_widens() -> None:
    # implementer wants {read_repo, write_code, run_tests, open_pr}; task only permits
    # read_repo + write_code -> result is the intersection (open_pr dropped).
    actions = set(
        resolve_allowed_actions(
            SubAgentRole.IMPLEMENTER,
            task_allowed=["read_repo", "write_code", "deploy_prod"],
            skill_allowed=frozenset(),
        )
    )
    assert actions == {"read_repo", "write_code"}
    assert "deploy_prod" not in actions  # task widening cannot grant non-role tools


def test_skill_allowlist_further_restricts() -> None:
    actions = set(
        resolve_allowed_actions(
            SubAgentRole.IMPLEMENTER,
            task_allowed=[],
            skill_allowed=frozenset({"read_repo"}),
        )
    )
    assert actions == {"read_repo"}


def test_resolve_max_parallel_is_min_of_positive() -> None:
    assert (
        resolve_max_parallel(
            SubagentRules(allow_subagents=True, max_parallel=2),
            SubAgentPolicy(allowed=True, max_parallel=3),
        )
        == 2
    )
    assert (
        resolve_max_parallel(
            SubagentRules(allow_subagents=True, max_parallel=0),
            SubAgentPolicy(allowed=True, max_parallel=0),
        )
        == 1
    )
