"""The policy's ``risk_rules`` block and what one rule means.

The models (and their load-time validation) live in ``scf_contracts`` because
``Policy`` carries them. This module adds the two things a rule does at
classification time: match paths. ``RiskRules.rule_ids()`` names the rules.
"""

from __future__ import annotations

from collections.abc import Iterable

from scf_contracts import RiskClassRequirements, RiskRule, RiskRules
from scf_contracts.conditions import matches_glob


def matches(rule: RiskRule, paths: Iterable[str]) -> bool:
    """True if any path matches any of the rule's globs.

    Uses the ``matches_glob`` policy-condition op, so a risk rule and a
    conditional policy rule cannot disagree about what a glob means.
    """
    return any(matches_glob(path, rule.paths) for path in paths)


__all__ = ["RiskClassRequirements", "RiskRule", "RiskRules", "matches"]
