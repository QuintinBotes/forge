"""Build the agent system prompt from an objective + repo context.

The system prompt is the agent's working context: the objective and instructions,
the active skill-profile behaviours (test-first, coverage floors, forbidden
shortcuts), acceptance criteria, the allowed/restricted action envelope, any
retrieved knowledge passed in ``objective.context``, and the repository's
``AGENTS.md`` narrative instructions.
"""

from __future__ import annotations

from typing import Any

from forge_contracts import AgentObjective, SkillProfile

__all__ = ["build_system_prompt", "skill_profile_directives"]


def skill_profile_directives(profile: SkillProfile) -> list[str]:
    """Render a skill profile into explicit behavioural directives."""
    directives: list[str] = [f"Skill profile: {profile.name}"]
    if profile.requires_plan:
        directives.append("- Produce an explicit plan before making changes.")
    if profile.requires_tests_before_implementation:
        directives.append("- Write tests BEFORE implementation (TDD).")
    if profile.min_test_coverage is not None:
        directives.append(f"- Maintain at least {profile.min_test_coverage}% test coverage.")
    if profile.verification_steps:
        steps = ", ".join(profile.verification_steps)
        directives.append(f"- Verification steps required: {steps}.")
    if profile.forbidden_shortcuts:
        directives.append(f"- Forbidden shortcuts: {', '.join(profile.forbidden_shortcuts)}.")
    if profile.forbidden_actions:
        directives.append(f"- Forbidden actions: {', '.join(profile.forbidden_actions)}.")
    if profile.review_required or profile.human_review_required:
        directives.append("- Human review is required before completion.")
    return directives


def _knowledge_section(context: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    knowledge = context.get("knowledge")
    if isinstance(knowledge, str) and knowledge.strip():
        parts.append("# Retrieved knowledge\n" + knowledge.strip())
    elif isinstance(knowledge, list) and knowledge:
        rendered = "\n".join(f"- {item}" for item in knowledge)
        parts.append("# Retrieved knowledge\n" + rendered)
    return parts


def build_system_prompt(
    objective: AgentObjective,
    *,
    agents_md: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Compose the full system prompt for a run."""
    context = context or {}
    parts: list[str] = [
        "You are a Forge execution agent. Operate a plan -> act -> observe loop: "
        "plan your approach, call tools to act, observe results, and finish by "
        "calling the 'finish' tool with your output and a confidence score.",
        f"Objective: {objective.objective}",
    ]
    if objective.description:
        parts.append(f"Description: {objective.description}")
    if objective.instructions:
        parts.append(f"Instructions: {objective.instructions}")
    if objective.skill_profile is not None:
        parts.append("\n".join(skill_profile_directives(objective.skill_profile)))
    if objective.acceptance_criteria:
        rendered = "\n".join(
            f"- {ac.id}: {ac.text}" for ac in objective.acceptance_criteria
        )
        parts.append("# Acceptance criteria\n" + rendered)
    if objective.allowed_actions:
        parts.append("Allowed actions: " + ", ".join(objective.allowed_actions))
    if objective.restricted_actions:
        parts.append(
            "Restricted actions (never perform these): "
            + ", ".join(objective.restricted_actions)
        )
    parts.extend(_knowledge_section(context))
    if agents_md:
        parts.append("# AGENTS.md (repository instructions)\n" + agents_md.strip())
    return "\n\n".join(parts)
