"""What a risk class demands of the shipping gates (spec A §3.2)."""

from __future__ import annotations

from scf_contracts import GateRequirements, RiskClass, RiskClassRequirements, RiskRules


def requirements_for(risk_class: RiskClass, rules: RiskRules) -> GateRequirements:
    """The approvals, deploy mode, red-team and self-eval the policy sets for ``risk_class``.

    A class the policy does not configure adds no requirement: risk only ever
    tightens a gate (spec A §4.1), so "nothing extra" is the safe reading.
    """
    configured = rules.classes.get(risk_class, RiskClassRequirements())
    return GateRequirements(
        risk_class=risk_class,
        approvals=configured.approvals,
        deploy_mode=configured.deploy,
        red_team=configured.red_team,
        self_eval=configured.self_eval,
    )


__all__ = ["requirements_for"]
