"""Benchmark suite & leaderboard models (F35).

Pure Pydantic models — no I/O, no SQLAlchemy, no FastAPI. They sit on top of
the *actual* in-tree eval substrate (``forge_eval.golden.GoldenCase`` + the
``forge_eval.metrics`` primitives).

Foundation-conformance note (deviation from the F35 draft): the draft assumed
an F12 ``forge_eval.models`` module shipping ``EvalReport`` / ``ReplayBundle`` /
``ArmConfig``. That substrate does not exist in-tree, so this module defines the
minimal equivalents the benchmark needs:

* :class:`BenchmarkReport` — the ``EvalReport`` stand-in: per-case results plus
  a per-metric aggregate whose entries expose ``.mean`` (the exact surface
  ``compute_benchmark_score`` reads in the slice spec).
* :class:`ReplayBundle` — a deterministic, content-hashed cassette of a case's
  *ordered outputs*. Replaying a bundle re-derives every metric offline (zero
  network), which is what makes leaderboard verification gaming-resistant.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

#: Version stamp for the on-disk manifest schema.
BENCHMARK_SCHEMA_VERSION = 1

#: Direction values accepted in :attr:`BenchmarkScoring.direction`.
HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"


class SubmissionStatus(StrEnum):
    """Lifecycle of a benchmark submission."""

    pending = "pending"
    scoring = "scoring"
    scored = "scored"
    verified = "verified"
    rejected = "rejected"
    flagged = "flagged"


class Visibility(StrEnum):
    """Whether a submission is visible on the public leaderboard."""

    private = "private"
    public = "public"


class BenchmarkScoring(BaseModel):
    """Frozen scoring rubric. ``metric_weights`` are normalized at scoring time."""

    primary_metric: str = "benchmark.composite"
    metric_weights: dict[str, float]
    direction: dict[str, str] = Field(default_factory=dict)
    category_field: str = "tags"
    #: Rank cutoff fed to the @k retrieval metrics during replay.
    k: int = 10

    @model_validator(mode="after")
    def _weights_positive(self) -> BenchmarkScoring:
        if not self.metric_weights or any(w <= 0 for w in self.metric_weights.values()):
            raise ValueError("metric_weights must be non-empty and strictly positive")
        for metric, direction in self.direction.items():
            if direction not in (HIGHER_IS_BETTER, LOWER_IS_BETTER):
                raise ValueError(f"invalid direction for {metric!r}: {direction!r}")
        return self


class BenchmarkManifest(BaseModel):
    """The ``manifest.yaml`` of one immutable benchmark version."""

    slug: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str = ""
    scoring: BenchmarkScoring
    case_files: list[str]
    schema_version: int = BENCHMARK_SCHEMA_VERSION
    content_hash: str | None = None
    frozen: bool = False

    @model_validator(mode="after")
    def _frozen_requires_hash(self) -> BenchmarkManifest:
        if self.frozen and not self.content_hash:
            raise ValueError("frozen manifest requires content_hash")
        return self


class MetricAggregate(BaseModel):
    """Mean of one metric over every case in a report."""

    mean: float
    count: int = 0


class BenchmarkCaseResult(BaseModel):
    """Per-case scoring inside a :class:`BenchmarkReport`."""

    case_id: str
    #: Normalized [0, 1] case score (weighted mean of the rubric metrics).
    score: float
    #: Raw (direction-unnormalized) per-metric values for this case.
    metrics: dict[str, float] = Field(default_factory=dict)
    passed: bool = True
    error: str | None = None


class BenchmarkReport(BaseModel):
    """Deterministic replay/report output an F35 score is computed from."""

    aggregate: dict[str, MetricAggregate] = Field(default_factory=dict)
    results: list[BenchmarkCaseResult] = Field(default_factory=list)


class ReplayBundle(BaseModel):
    """A content-hashed cassette of one case's ordered outputs.

    ``output_ids`` is the ordered id list the submitter's pipeline produced for
    the case (best first). ``content_hash`` is ``sha256:<hex>`` over the
    canonical JSON of ``{case_id, output_ids}`` — recomputed during
    verification, so a tampered bundle can never match its claimed hash.
    """

    case_id: str
    output_ids: list[str] = Field(default_factory=list)
    content_hash: str = ""


class CategoryScore(BaseModel):
    """Score breakdown for one benchmark category."""

    category: str
    score: float
    weight: float
    case_count: int


class BenchmarkScore(BaseModel):
    """The deterministic, weighted composite an entry is ranked by."""

    composite: float
    per_metric: dict[str, float] = Field(default_factory=dict)
    per_category: list[CategoryScore] = Field(default_factory=list)
    total_cases: int = 0
    passed: int = 0
    errored: int = 0


class VerificationResult(BaseModel):
    """Outcome of deterministically re-deriving a claimed score from bundles."""

    verified: bool
    claimed_composite: float
    reproduced_composite: float
    score_delta: float
    epsilon: float
    bundle_hash_matches: bool
    reasons: list[str] = Field(default_factory=list)


class LeaderboardRow(BaseModel):
    """One ranked public leaderboard entry."""

    rank: int
    submission_id: UUID
    model_label: str
    agent_mode: str
    composite_score: float
    verified: bool
    forge_version: str | None = None
    submitter_name: str
    submitter_org: str | None = None
    per_category: list[CategoryScore] = Field(default_factory=list)
    submitted_at: datetime


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "BenchmarkCaseResult",
    "BenchmarkManifest",
    "BenchmarkReport",
    "BenchmarkScore",
    "BenchmarkScoring",
    "CategoryScore",
    "LeaderboardRow",
    "MetricAggregate",
    "ReplayBundle",
    "SubmissionStatus",
    "VerificationResult",
    "Visibility",
]
