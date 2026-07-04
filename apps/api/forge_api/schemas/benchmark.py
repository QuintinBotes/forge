"""Request/response schemas for the F35 benchmark & leaderboard routes.

The ``Public*`` models are the structural privacy boundary of the public,
unauthenticated surface: they cannot carry ``submitter_contact``, raw config,
or raw payloads — a field that is not declared is never serialized.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from forge_eval.benchmark import (
    BenchmarkScore,
    CategoryScore,
    ReplayBundle,
    SubmissionStatus,
    VerificationResult,
    Visibility,
)

# --------------------------------------------------------------------------- #
# Authenticated management                                                      #
# --------------------------------------------------------------------------- #


class SubmitBenchmarkRequest(BaseModel):
    """External-contributor submission: claimed score + deterministic bundles."""

    submitter_name: str = Field(min_length=1, max_length=255)
    submitter_org: str | None = None
    #: PRIVATE (email/url) — stored, never returned by any response model.
    submitter_contact: str | None = None
    model_label: str = Field(min_length=1, max_length=255)
    agent_mode: str = "single_agent"
    forge_version: str | None = None
    #: Redacted by the service before persist; BYOK keys never survive ingest.
    config: dict[str, Any] = Field(default_factory=dict)
    #: Optional drift guard: when provided it must equal the suite's hash (409).
    suite_content_hash: str | None = None
    claimed: BenchmarkScore
    bundles: list[ReplayBundle]


class BenchmarkSuiteOut(BaseModel):
    slug: str
    version: str
    title: str
    description: str
    task_count: int
    primary_metric: str
    content_hash: str
    frozen: bool
    published: bool


class SubmissionOut(BaseModel):
    """Authenticated (workspace-scoped) view. ``submitter_contact`` is
    intentionally excluded from every response model."""

    id: UUID
    benchmark_slug: str
    benchmark_version: str
    model_label: str
    agent_mode: str
    forge_version: str | None
    composite_score: float | None
    scores: BenchmarkScore | None
    status: SubmissionStatus
    visibility: Visibility
    verified: bool
    verification: VerificationResult | None
    submitter_name: str
    submitter_org: str | None
    submitted_at: datetime


class PublishRequest(BaseModel):
    force: bool = False


class FlagRequest(BaseModel):
    reason: str = Field(min_length=1)


class VerifyResponse(BaseModel):
    submission_id: UUID
    verification: VerificationResult
    status: SubmissionStatus


# --------------------------------------------------------------------------- #
# Public (unauthenticated) — payload-free, secret-free                          #
# --------------------------------------------------------------------------- #


class PublicBenchmarkOut(BaseModel):
    slug: str
    version: str
    title: str
    description: str
    task_count: int
    primary_metric: str
    content_hash: str


class PublicLeaderboardEntry(BaseModel):
    rank: int
    model_label: str
    agent_mode: str
    composite_score: float
    verified: bool
    forge_version: str | None
    submitter_name: str
    submitter_org: str | None
    per_category: list[CategoryScore]
    submitted_at: datetime
    submission_id: UUID


class PublicLeaderboard(BaseModel):
    slug: str
    version: str
    title: str
    primary_metric: str
    content_hash: str
    generated_at: datetime
    entries: list[PublicLeaderboardEntry]


class PublicSubmissionDetail(BaseModel):
    submission_id: UUID
    slug: str
    version: str
    model_label: str
    agent_mode: str
    forge_version: str | None
    composite_score: float
    verified: bool
    #: Breakdown only — no raw payloads.
    scores: BenchmarkScore
    submitter_name: str
    submitter_org: str | None
    submitted_at: datetime
    #: The exact offline reproduce invocation.
    reproduce_command: str
    #: Where the deterministic replay bundles can be fetched (payload-free).
    replay_bundle_urls: list[str]


__all__ = [
    "BenchmarkSuiteOut",
    "FlagRequest",
    "PublicBenchmarkOut",
    "PublicLeaderboard",
    "PublicLeaderboardEntry",
    "PublicSubmissionDetail",
    "PublishRequest",
    "SubmissionOut",
    "SubmitBenchmarkRequest",
    "VerifyResponse",
]
