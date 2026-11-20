"""Test doubles for the contracts' protocols (as ``scf_agent.testing`` ships its own)."""

from __future__ import annotations

import uuid
from typing import Any

from scf_contracts.risk import (
    ClassifiedChange,
    GatedChange,
    GateRequirements,
    RiskClass,
    RiskEnforcement,
    RiskGate,
)

#: The strictest requirements a class can carry.
STRICT = GateRequirements(
    risk_class=RiskClass.CRITICAL, approvals=3, deploy_mode="gated", red_team=True, self_eval=True
)


class RecordingRiskReader:
    """A ``RiskReader`` that classifies every change; it records lookups and observations."""

    def __init__(self, mode: RiskEnforcement = "shadow", **requirements: Any) -> None:
        self.mode = mode
        self.requirements = STRICT.model_copy(update=requirements)
        self.looked_up: list[GatedChange] = []
        self.observed: list[dict[str, str]] = []

    def risk_for(self, change: GatedChange) -> ClassifiedChange:
        self.looked_up.append(change)
        return ClassifiedChange(
            classification_id=uuid.uuid4(),
            workspace_id=change.workspace_id,
            enforcement=self.mode,
            requirements=self.requirements,
        )

    def observe(
        self, risk: ClassifiedChange, *, gate: RiskGate, actual: str, risk_decision: str
    ) -> None:
        self.observed.append({"gate": gate, "actual": actual, "risk_decision": risk_decision})

    def assert_observed_each_lookup(self, gate: RiskGate, decisions: set[str]) -> None:
        """One observation per evaluation, each naming ``gate`` and two of ``decisions``."""
        assert len(self.observed) == len(self.looked_up)
        for seen in self.observed:
            assert seen["gate"] == gate
            assert {seen["actual"], seen["risk_decision"]} <= decisions


__all__ = ["STRICT", "RecordingRiskReader"]
