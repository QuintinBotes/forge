"""Typed risk errors.

Invalid ``risk_rules`` are rejected when the policy loads (the contract models
validate, so ``scf_policy.load_policy`` raises its usual ``ValidationError``).
The only error raised after that is a refused de-escalation; ``derive`` never raises.
"""

from __future__ import annotations

from scf_contracts import RiskClass, ScfError


class RiskDeEscalationError(ScfError):
    """A class was declared below the derived floor without an override."""

    def __init__(self, derived: RiskClass, declared: RiskClass) -> None:
        self.derived = derived
        self.declared = declared
        super().__init__(
            f"declared risk class {declared.value!r} is below the derived class "
            f"{derived.value!r}; lowering it needs an override with an approver and a reason"
        )


__all__ = ["RiskDeEscalationError"]
