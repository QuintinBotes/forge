"""Runbook blast-radius safety helper — the structural remediation guarantee (F17).

``assert_runbook_within_policy`` is the single, pure, total function that decides
whether a proposed remediation runbook is within the ``incident-response`` posture.
A non-empty result MUST block plan approval and route the run to escalation. It is
a thin composition over the F11 skill-directives gate (``skill_permits_action`` +
``ACTION_ALIASES``) and the blast-radius cap.
"""

from __future__ import annotations

from forge_contracts.incident import BLAST_ORDER, Runbook, blast_rank
from forge_skill import SkillDirectives, blast_within, skill_permits_action

__all__ = ["BLAST_ORDER", "assert_runbook_within_policy", "runbook_max_blast_radius"]


def assert_runbook_within_policy(runbook: Runbook, directives: SkillDirectives) -> list[str]:
    """Return the ids of steps that VIOLATE the incident-response posture.

    A step violates the posture when:

    * its action (or an alias) is forbidden, or
    * the directives carry a non-empty allowlist that does not cover the action, or
    * its blast radius exceeds ``directives.max_blast_radius``.

    Pure and total: never raises, always returns a ``list[str]`` (empty == OK).
    """
    offending: list[str] = []
    for step in runbook.steps:
        decision = skill_permits_action(directives, step.action)
        if not decision.allowed:
            offending.append(step.id)
            continue
        if not blast_within(step.blast_radius, directives.max_blast_radius):
            offending.append(step.id)
    return offending


def runbook_max_blast_radius(runbook: Runbook) -> str:
    """Return the maximum blast-radius value across the runbook's steps."""
    if not runbook.steps:
        return "low"
    return max((s.blast_radius for s in runbook.steps), key=blast_rank)
