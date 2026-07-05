"""Submission verification: reproduce the claimed score or reject (F35 §4).

Pure function — the deterministic replay has already been executed by the
caller (:func:`forge_eval.benchmark.replay.replay_bundles`). Verification
passes iff the reproduced composite matches the claimed composite within
``epsilon`` AND every recomputed bundle ``content_hash`` matches the hashes the
submission shipped (gaming-resistance, AC8-AC10).
"""

from __future__ import annotations

from collections.abc import Sequence

from forge_eval.benchmark.models import (
    BenchmarkReport,
    BenchmarkScore,
    BenchmarkScoring,
    ReplayBundle,
    VerificationResult,
)
from forge_eval.benchmark.replay import compute_bundle_hash
from forge_eval.benchmark.scoring import compute_benchmark_score
from forge_eval.golden import GoldenCase

__all__ = ["verify_submission"]


def verify_submission(
    *,
    claimed: BenchmarkScore,
    reproduced_report: BenchmarkReport,
    reproduced_bundles: Sequence[ReplayBundle],
    claimed_bundle_hashes: Sequence[str],
    scoring: BenchmarkScoring,
    cases: Sequence[GoldenCase],
    epsilon: float,
) -> VerificationResult:
    """Deterministically re-derive the score and compare it to the claim."""
    reproduced = compute_benchmark_score(reproduced_report, scoring, cases)
    delta = abs(reproduced.composite - claimed.composite)
    reasons: list[str] = []

    recomputed_hashes = [
        compute_bundle_hash(bundle.case_id, bundle.output_ids) for bundle in reproduced_bundles
    ]
    bundle_hash_matches = recomputed_hashes == list(claimed_bundle_hashes)
    if len(recomputed_hashes) != len(claimed_bundle_hashes):
        reasons.append(
            f"bundle count mismatch: submission shipped {len(claimed_bundle_hashes)} "
            f"hash(es) but {len(recomputed_hashes)} bundle(s) replayed"
        )
    elif not bundle_hash_matches:
        mismatched = [
            bundle.case_id
            for bundle, recomputed, claimed_hash in zip(
                reproduced_bundles, recomputed_hashes, claimed_bundle_hashes, strict=True
            )
            if recomputed != claimed_hash
        ]
        reasons.append(
            "bundle content_hash mismatch (tampered or corrupted bundle) for case(s): "
            + ", ".join(mismatched)
        )

    score_ok = delta <= epsilon
    if not score_ok:
        reasons.append(
            f"claimed composite {claimed.composite:.6f} differs from reproduced "
            f"{reproduced.composite:.6f} by {delta:.6f} > epsilon {epsilon:.6f}"
        )

    return VerificationResult(
        verified=score_ok and bundle_hash_matches,
        claimed_composite=claimed.composite,
        reproduced_composite=reproduced.composite,
        score_delta=round(delta, 6),
        epsilon=epsilon,
        bundle_hash_matches=bundle_hash_matches,
        reasons=reasons,
    )
