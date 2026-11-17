"""Evaluate a declared risk class against the derived floor (spec A §3.1).

Declaring higher is free. Declaring lower needs a :class:`RiskOverride` naming
an approver and a reason (its model rejects blank ones); without one the change
is refused, never silently downgraded.
"""

from __future__ import annotations

from scf_contracts import DerivedRisk, RiskClass, RiskEvaluation, RiskOverride
from scf_risk.errors import RiskDeEscalationError


def evaluate(
    derived: DerivedRisk,
    declared: RiskClass | None,
    override: RiskOverride | None = None,
) -> RiskEvaluation:
    """The final class: the floor, a higher declaration, or an overridden lower one.

    Raises:
        RiskDeEscalationError: ``declared`` is below the floor and no override is given.
    """
    floor = derived.risk_class
    if declared is None or declared >= floor:
        final = floor if declared is None else declared
        override = None  # nothing was lowered, so there is no override to record
    elif override is None:
        raise RiskDeEscalationError(floor, declared)
    else:
        final = declared
    return RiskEvaluation(
        derived_class=floor,
        declared_class=declared,
        final_class=final,
        matched_rule_ids=derived.matched_rule_ids,
        fallback_applied=derived.fallback_applied,
        override_approver=override.approver if override else None,
        override_reason=override.reason if override else None,
    )


__all__ = ["evaluate"]
