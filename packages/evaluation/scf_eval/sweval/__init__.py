"""Self-Eval Gate (F41) sandboxed fail-to-pass / pass-to-pass runner.

A minted Self-Eval Gate case carries HIDDEN tests (``fail_to_pass`` /
``pass_to_pass``) that never enter a model's context. :func:`run_swe_case`
applies a candidate patch inside a sandbox, runs those hidden tests through the
:class:`~scf_contracts.sandbox.SandboxSession` execution seam, and scores the
resolution rate — the ground-truth signal the Self-Eval Gate blocks on.

:func:`run_suite` runs one solver over a whole forge-factory-bench suite on the same
runner and writes a report; :class:`ScfInstanceSolver` drives a running SCF instance.
"""

from __future__ import annotations

from scf_eval.sweval.bench import (
    BenchCaseResult,
    BenchRunReport,
    CaseOutcome,
    FnSolver,
    Solver,
    empty_solver,
    reference_solver,
    run_suite,
)
from scf_eval.sweval.gate import SelfEvalGate, SelfEvalRegressionError
from scf_eval.sweval.runner import SweCaseResult, run_swe_case
from scf_eval.sweval.scf_solver import ScfInstanceSolver
from scf_eval.sweval.self_eval import SelfEvalScorecard, run_self_eval

__all__ = [
    "BenchCaseResult",
    "BenchRunReport",
    "CaseOutcome",
    "FnSolver",
    "ScfInstanceSolver",
    "SelfEvalGate",
    "SelfEvalRegressionError",
    "SelfEvalScorecard",
    "Solver",
    "SweCaseResult",
    "empty_solver",
    "reference_solver",
    "run_self_eval",
    "run_suite",
    "run_swe_case",
]
