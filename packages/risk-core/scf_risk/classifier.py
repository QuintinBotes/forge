"""Derive a change's risk floor from its diff facts (spec A §3.1, §4.2).

Pure, total and deterministic: no I/O, no clock, and no input makes it raise.
"""

from __future__ import annotations

from scf_contracts import ChangeSignals, DerivedRisk, RiskRules
from scf_risk.rules import matches, rule_id


def derive(signals: ChangeSignals, rules: RiskRules) -> DerivedRisk:
    """``max(class of every matching rule, default)``, or ``fallback`` when signals are missing.

    The policy default is the floor: a matching rule can raise the class, never
    lower it, so adding a rule never makes a change less risky.
    """
    if not signals.available:
        return DerivedRisk(
            risk_class=rules.fallback,
            fallback_applied=True,
            reason=signals.unavailable_reason or "change signals unavailable",
        )
    matched = [
        (rule_id(rule, index), rule.risk_class)
        for index, rule in enumerate(rules.derive)
        if matches(rule, signals.paths)
    ]
    return DerivedRisk(
        risk_class=max([rules.default, *(risk_class for _, risk_class in matched)]),
        matched_rule_ids=[matched_id for matched_id, _ in matched],
    )


__all__ = ["derive"]
