"""The policy's ``risk_rules`` block and what one rule means.

The models (and their load-time validation) live in ``scf_contracts`` because
``Policy`` carries them. This module adds the two things a rule does at
classification time: name itself and match paths.
"""

from __future__ import annotations

from collections.abc import Iterable

from scf_contracts import RiskClassRequirements, RiskRule, RiskRules
from scf_contracts.conditions import matches_glob


def rule_id(rule: RiskRule, index: int) -> str:
    """The rule's ``id``, or ``derive[<index>]`` when the policy gave none."""
    return rule.id or f"derive[{index}]"


def matches(rule: RiskRule, paths: Iterable[str]) -> bool:
    """True if any path matches any of the rule's globs.

    Uses the ``matches_glob`` policy-condition op, so a risk rule and a
    conditional policy rule cannot disagree about what a glob means.
    """
    return any(matches_glob(path, rule.paths) for path in paths)


__all__ = ["RiskClassRequirements", "RiskRule", "RiskRules", "matches", "rule_id"]
