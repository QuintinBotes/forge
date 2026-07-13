"""Self-Eval Gate (F41) sandboxed fail-to-pass / pass-to-pass runner.

A minted Self-Eval Gate case carries HIDDEN tests (``fail_to_pass`` /
``pass_to_pass``) that never enter a model's context. :func:`run_swe_case`
applies a candidate patch inside a sandbox, runs those hidden tests through the
:class:`~forge_contracts.sandbox.SandboxSession` execution seam, and scores the
resolution rate — the ground-truth signal the Self-Eval Gate blocks on.
"""

from __future__ import annotations

from forge_eval.sweval.runner import SweCaseResult, run_swe_case
from forge_eval.sweval.self_eval import SelfEvalScorecard, run_self_eval

__all__ = ["SelfEvalScorecard", "SweCaseResult", "run_self_eval", "run_swe_case"]
