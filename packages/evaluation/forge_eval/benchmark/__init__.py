"""F35 — frozen benchmark suites, deterministic scoring, verification, ranking.

Pure, framework-free extension of ``forge_eval`` (no FastAPI/SQLAlchemy). The
API service (``forge_api.services.benchmark_service``) wires these primitives
to persistence and moderation; everything here is deterministic and offline.
"""

from __future__ import annotations

from forge_eval.benchmark.errors import (
    BenchmarkContentHashMismatch,
    BenchmarkError,
    BenchmarkFrozenError,
    BenchmarkVerificationError,
)
from forge_eval.benchmark.leaderboard import rank_submissions
from forge_eval.benchmark.manifest import (
    MANIFEST_FILENAME,
    compute_content_hash,
    freeze,
    load_manifest,
    validate_freezable,
)
from forge_eval.benchmark.models import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkCaseResult,
    BenchmarkManifest,
    BenchmarkReport,
    BenchmarkScore,
    BenchmarkScoring,
    CategoryScore,
    LeaderboardRow,
    MetricAggregate,
    ReplayBundle,
    SubmissionStatus,
    VerificationResult,
    Visibility,
)
from forge_eval.benchmark.replay import (
    METRIC_REGISTRY,
    compute_bundle_hash,
    make_bundle,
    replay_bundles,
)
from forge_eval.benchmark.scoring import compute_benchmark_score
from forge_eval.benchmark.swe_case import SweCaseFields, parse_swe_case_fields
from forge_eval.benchmark.verify import verify_submission

__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "METRIC_REGISTRY",
    "BenchmarkCaseResult",
    "BenchmarkContentHashMismatch",
    "BenchmarkError",
    "BenchmarkFrozenError",
    "BenchmarkManifest",
    "BenchmarkReport",
    "BenchmarkScore",
    "BenchmarkScoring",
    "BenchmarkVerificationError",
    "CategoryScore",
    "LeaderboardRow",
    "MetricAggregate",
    "ReplayBundle",
    "SubmissionStatus",
    "SweCaseFields",
    "VerificationResult",
    "Visibility",
    "compute_benchmark_score",
    "compute_bundle_hash",
    "compute_content_hash",
    "freeze",
    "load_manifest",
    "make_bundle",
    "parse_swe_case_fields",
    "rank_submissions",
    "replay_bundles",
    "validate_freezable",
    "verify_submission",
]
