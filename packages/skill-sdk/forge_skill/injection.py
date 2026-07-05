"""Fold a skill profile's structural constraints into an ``AgentObjective``.

Skill profiles enforce quality structurally rather than via prompt text
(FORGE_SPEC.md, "Core Principles"). ``inject_profile`` therefore returns a *new*
objective in which the profile's behaviour is materialised on concrete fields:

* the full profile is attached (``skill_profile``) so every behaviour travels
  with the objective;
* the profile's ``allowed_actions`` / ``forbidden_actions`` are merged
  (order-preserving, de-duplicated) into the objective's allow / restrict lists;
* profiles demanding human oversight tighten the approval gates; and
* the structural directives (plan/test/coverage/shortcut rules) are flattened
  under ``context["skill"]`` for downstream consumers (workflow + verifier).

The input objective is never mutated.
"""

from __future__ import annotations

from collections.abc import Iterable

from forge_contracts import AgentObjective, SkillProfile

__all__ = ["inject_profile"]

# Key under which flattened structural directives are exposed on the objective.
_DIRECTIVES_KEY = "skill"


def _ordered_union(*sequences: Iterable[str]) -> list[str]:
    """Merge sequences preserving first-seen order and dropping duplicates."""
    seen: dict[str, None] = {}
    for sequence in sequences:
        for item in sequence:
            seen.setdefault(item, None)
    return list(seen)


def _directives(profile: SkillProfile) -> dict[str, object]:
    """Flatten a profile's structural constraints for downstream consumers."""
    return {
        "profile": profile.name,
        "requires_plan": profile.requires_plan,
        "requires_tests_before_implementation": (profile.requires_tests_before_implementation),
        "min_test_coverage": profile.min_test_coverage,
        "verification_steps": list(profile.verification_steps),
        "review_required": profile.review_required,
        "forbidden_shortcuts": list(profile.forbidden_shortcuts),
        "accessibility_check": profile.accessibility_check,
        "requires_human_approval_before_action": (profile.requires_human_approval_before_action),
        "human_review_required": profile.human_review_required,
        "max_blast_radius": profile.max_blast_radius,
        "output_type": profile.output_type,
        "report_format": profile.report_format,
        "tools": list(profile.tools),
    }


def inject_profile(profile: SkillProfile, context: AgentObjective) -> AgentObjective:
    """Return a copy of ``context`` with ``profile``'s behaviour injected."""
    objective = context.model_copy(deep=True)

    objective.skill_profile = profile.model_copy(deep=True)
    objective.allowed_actions = _ordered_union(context.allowed_actions, profile.allowed_actions)
    objective.restricted_actions = _ordered_union(
        context.restricted_actions, profile.forbidden_actions
    )

    if profile.requires_human_approval_before_action or profile.human_review_required:
        gates = objective.requires_approval.model_copy()
        gates.pr = True
        gates.deploy = True
        objective.requires_approval = gates

    objective.context = {**context.context, _DIRECTIVES_KEY: _directives(profile)}
    return objective
