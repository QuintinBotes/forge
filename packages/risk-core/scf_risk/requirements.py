"""What a risk class demands of the shipping gates (spec A §3.2)."""

from __future__ import annotations

from scf_contracts import GateRequirements, RiskClass, RiskClassRequirements, RiskRules


def requirements_for(risk_class: RiskClass, rules: RiskRules) -> GateRequirements:
    """The approvals, deploy mode, red-team and self-eval the policy sets for ``risk_class``.

    A class the policy does not configure takes the requirements of the highest
    configured class below it, so a critical change (every fallback is one) never
    gets fewer controls than a high one. With no configured class at or below it,
    the class adds no requirement.
    """
    at_or_below = [configured for configured in rules.classes if configured <= risk_class]
    configured = rules.classes[max(at_or_below)] if at_or_below else RiskClassRequirements()
    return GateRequirements(
        risk_class=risk_class,
        approvals=configured.approvals,
        deploy_mode=configured.deploy,
        red_team=configured.red_team,
        self_eval=configured.self_eval,
    )


__all__ = ["requirements_for"]
