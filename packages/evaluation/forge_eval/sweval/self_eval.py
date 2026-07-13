"""Aggregate a Self-Eval run over a workspace's private benchmark suite.

Runs every minted case through :func:`run_swe_case` with the candidate config's
``solve_fn`` and reports the resolution rate — the ground-truth signal the
Self-Eval Gate blocks a model/prompt/router change on. Pure and injectable:
``solve_fn`` and the per-case worktree factory are passed in, so the API layer
wires the real agent runtime + sandbox while tests use a scripted model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from forge_contracts.sandbox import SandboxProvider
from forge_eval.golden import GoldenCase
from forge_eval.sweval.runner import SolveFn, SweCaseResult, run_swe_case

#: Prepare (or look up) a worktree checked out at the case's base_commit and
#: return its host path.
WorktreeFactory = Callable[[GoldenCase], str]


@dataclass(frozen=True)
class SelfEvalScorecard:
    """The result of a Self-Eval run over a private suite."""

    total: int
    resolved: int
    resolution_rate: float
    per_case: list[SweCaseResult] = field(default_factory=list)

    def meets(self, baseline_rate: float) -> bool:
        """True iff this run does NOT regress vs a recorded baseline rate."""
        return self.resolution_rate >= baseline_rate


async def run_self_eval(
    *,
    cases: Sequence[GoldenCase],
    solve_fn: SolveFn,
    sandbox_provider: SandboxProvider,
    worktree_for: WorktreeFactory,
) -> SelfEvalScorecard:
    """Score ``solve_fn`` (the config under test) over every case in the suite."""
    results: list[SweCaseResult] = []
    for case in cases:
        results.append(
            await run_swe_case(
                case=case,
                solve_fn=solve_fn,
                sandbox_provider=sandbox_provider,
                worktree_path=worktree_for(case),
            )
        )
    total = len(results)
    resolved = sum(1 for r in results if r.resolved)
    rate = resolved / total if total else 1.0
    return SelfEvalScorecard(total=total, resolved=resolved, resolution_rate=rate, per_case=results)
