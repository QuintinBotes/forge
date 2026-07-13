"""Benchmark suite & leaderboard-submission models (F35).

Two tables:

* :class:`BenchmarkSuite` — the registration + freeze record for one immutable
  benchmark version. The canonical task set lives in versioned YAML files
  (``packages/evaluation/forge_eval/benchmarks/<slug>/<version>/``); the row
  binds the frozen ``content_hash`` so every submission is comparable.
* :class:`BenchmarkSubmission` — one scored attempt of a frozen suite under a
  configuration. Publicly rankable only after admin moderation
  (``visibility='public'``).

Foundation conformance (deviations from the F35 draft): the draft assumed F12
``eval_run``/``replay_bundle`` tables and a MinIO bundle store — neither exists
in-tree, so there is no ``eval_run_id`` FK and the deterministic replay bundles
are persisted inline in ``replay_bundles`` (JSON, size-capped at ingest).
``benchmark_suite`` is global (no ``workspace_id``) by default: a frozen suite
is a shared community artifact, like the file-based golden sets. Submissions
carry a *nullable* ``workspace_id`` (NULL = official/system submission).
Status / visibility are stored as plain strings guarded by CHECK constraints,
matching the marketplace/deployment precedent.

Self-Eval Gate (F41) extends ``benchmark_suite`` with three *nullable*
columns so an org can mint a PRIVATE, per-repo regression suite from its own
merged PRs, without disturbing the existing global/public suites (which keep
``workspace_id``/``repo_id`` NULL and ``private=False``):

* ``workspace_id`` — NULL = shared/community suite (unchanged default);
  non-NULL = scoped to one workspace's own benchmark.
* ``repo_id`` — the source repository the suite was minted from (free-form
  provider identifier, mirroring ``RepositoryConnection.repo_id`` — no FK,
  since suites can outlive a disconnected repository).
* ``private`` — when true, the suite (and its submissions) must never be
  surfaced by ``/public/*`` leaderboard endpoints, regardless of submission
  ``visibility``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import ForgeModel, json_type


class BenchmarkSuite(ForgeModel):
    """Registration + freeze record for one immutable benchmark version."""

    __tablename__ = "benchmark_suite"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_benchmark_suite_slug_version"),
        Index("ix_benchmark_suite_published_slug", "published", "slug"),
        Index("ix_benchmark_suite_workspace_id", "workspace_id"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    primary_metric: Mapped[str] = mapped_column(
        String(128), default="benchmark.composite", nullable=False
    )
    #: Serialized ``forge_eval.benchmark.BenchmarkScoring`` (weights, direction).
    scoring: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    #: sha256 over the canonical ordered cases + scoring — the reproducibility anchor.
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    frozen: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Suite is selectable on the public leaderboard.
    published: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    #: NULL = shared/community suite (unscoped, matches prior behavior).
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=True,
    )
    #: Source repository the suite was minted from (free-form provider
    #: identifier, e.g. ``"github:org/repo"``); no FK — outlives disconnects.
    repo_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Self-Eval Gate suites are private: never surfaced by ``/public/*``.
    private: Mapped[bool] = mapped_column(default=False, nullable=False)


class BenchmarkSubmission(ForgeModel):
    """One scored attempt of a frozen suite under a configuration."""

    __tablename__ = "benchmark_submission"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','scoring','scored','verified','rejected','flagged')",
            name="submission_status",
        ),
        CheckConstraint("visibility IN ('private','public')", name="submission_visibility"),
        # The leaderboard covering index.
        Index(
            "ix_benchmark_submission_leaderboard",
            "benchmark_suite_id",
            "visibility",
            "verified",
            "composite_score",
        ),
        Index(
            "ix_benchmark_submission_workspace_submitted",
            "workspace_id",
            "submitted_at",
        ),
        Index("ix_benchmark_submission_status", "status"),
    )

    benchmark_suite_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("benchmark_suite.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Copied at submit time; must equal the suite's hash (drift guard).
    suite_content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    #: NULL = official/system submission (admin-managed, not tenant-scoped).
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=True,
    )
    submitter_name: Mapped[str] = mapped_column(String(255), nullable=False)
    submitter_org: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: PRIVATE — never serialized by any ``/public/*`` response model.
    submitter_contact: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Public family label, e.g. ``claude-opus / anthropic`` (no keys).
    model_label: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_mode: Mapped[str] = mapped_column(String(64), default="single_agent", nullable=False)
    #: Redacted run configuration — secrets are stripped at ingest, never stored.
    config: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    forge_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The leaderboard number (0..1); NULL until scored.
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Serialized ``BenchmarkScore`` (per-metric + per-category breakdown).
    scores: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    #: Inline deterministic replay bundles (foundation has no object store).
    replay_bundles: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    #: Ordered bundle content hashes claimed at submit time (verification input).
    replay_content_hashes: Mapped[list[Any]] = mapped_column(
        json_type(), default=list, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    visibility: Mapped[str] = mapped_column(String(8), default="private", nullable=False)
    verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Serialized ``VerificationResult`` (claimed vs reproduced, deltas, reasons).
    verification: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    moderated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["BenchmarkSubmission", "BenchmarkSuite"]
