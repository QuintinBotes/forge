"""Risk classification (spec A): derive a change's risk class and evaluate a declared one.

Pure and I/O-free. The models live in ``scf_contracts``; this package computes.
"""

from __future__ import annotations

from scf_risk.classifier import derive
from scf_risk.errors import RiskDeEscalationError
from scf_risk.evaluation import evaluate
from scf_risk.requirements import requirements_for

__version__ = "0.1.0"

__all__ = ["RiskDeEscalationError", "derive", "evaluate", "requirements_for"]
