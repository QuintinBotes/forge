"""Unit tests for system-prompt assembly (``forge_agent.context``)."""

from __future__ import annotations

from forge_agent.context import build_system_prompt, skill_profile_directives
from forge_contracts import AcceptanceCriterion, AgentObjective, SkillProfile


def test_skill_profile_directives_render_all_flags() -> None:
    profile = SkillProfile(
        name="strict",
        requires_plan=True,
        requires_tests_before_implementation=True,
        min_test_coverage=90,
        verification_steps=["lint", "test"],
        forbidden_shortcuts=["skip-tests"],
        forbidden_actions=["deploy_prod"],
        review_required=True,
    )
    joined = "\n".join(skill_profile_directives(profile))
    assert "Skill profile: strict" in joined
    assert "explicit plan" in joined
    assert "TDD" in joined
    assert "90% test coverage" in joined
    assert "lint, test" in joined
    assert "skip-tests" in joined
    assert "deploy_prod" in joined
    assert "Human review is required" in joined


def test_skill_profile_directives_minimal_profile() -> None:
    # A bare profile renders only its name line; all optional sections are skipped.
    directives = skill_profile_directives(SkillProfile(name="bare"))
    assert directives == ["Skill profile: bare"]


def test_build_system_prompt_includes_all_sections() -> None:
    objective = AgentObjective(
        objective="Add feature",
        description="A described task",
        instructions="Be careful",
        skill_profile=SkillProfile(name="p", requires_plan=True),
        acceptance_criteria=[AcceptanceCriterion(id="A1", text="works")],
        allowed_actions=["read_repo"],
        restricted_actions=["deploy_prod"],
    )
    prompt = build_system_prompt(
        objective,
        agents_md="# repo rules\nbe nice\n",
        context={"knowledge": "some retrieved text"},
    )
    assert "Description: A described task" in prompt
    assert "Instructions: Be careful" in prompt
    assert "Skill profile: p" in prompt
    assert "A1: works" in prompt
    assert "Allowed actions: read_repo" in prompt
    assert "Restricted actions" in prompt
    assert "deploy_prod" in prompt
    assert "# Retrieved knowledge" in prompt
    assert "some retrieved text" in prompt
    assert "AGENTS.md" in prompt
    assert "be nice" in prompt


def test_knowledge_section_accepts_list() -> None:
    prompt = build_system_prompt(
        AgentObjective(objective="x"),
        context={"knowledge": ["fact one", "fact two"]},
    )
    assert "# Retrieved knowledge" in prompt
    assert "- fact one" in prompt
    assert "- fact two" in prompt


def test_build_system_prompt_minimal_objective_omits_optionals() -> None:
    prompt = build_system_prompt(AgentObjective(objective="just this"))
    assert "Objective: just this" in prompt
    assert "Description:" not in prompt
    assert "Instructions:" not in prompt
    assert "Retrieved knowledge" not in prompt
    assert "AGENTS.md" not in prompt
