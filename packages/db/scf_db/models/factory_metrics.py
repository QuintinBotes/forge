"""Evidence for the five factory metrics (spec 005 FR-009, #196).

``change_outcome`` holds the final state of an attempt's pull request and any
later revert; ``run_intervention`` holds the interventions SCF learns of from
GitHub deliveries (a review that requests changes, a commit by another identity).
Both are written by the GitHub webhook, keyed so a redelivery writes nothing new.
Gate rejections and escalations are read from ``approval_request`` and
``agent_run`` where they are already recorded. ``docs/metrics/factory-metrics.md``
defines the metrics over these rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from scf_db.base import WorkspaceScopedModel

__all__ = [
    "CHANGE_CLOSED",
    "CHANGE_LANDED",
    "ChangeOutcome",
    "RunIntervention",
]

#: The pull request was merged.
CHANGE_LANDED = "landed"
#: The pull request was closed without being merged.
CHANGE_CLOSED = "closed"


class ChangeOutcome(WorkspaceScopedModel):
    """The final outcome of one attempt's pull request, and its revert if any."""

    __tablename__ = "change_outcome"
    __table_args__ = (
        Index("ix_change_outcome_pull_request", "repo_full_name", "pr_number"),
        Index("ix_change_outcome_merge_commit", "merge_commit_sha"),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``landed`` | ``closed``.
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    #: When it was merged, or closed without merging.
    outcome_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When a later landed pull request reverted it; never cleared.
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_by_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The full hash of the commit a landed pull request put on its base branch (#335):
    #: the name a deploy usually gives the change, whose classification has the head's.
    merge_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RunIntervention(WorkspaceScopedModel):
    """One intervention by a person on an attempt, from a GitHub delivery."""

    __tablename__ = "run_intervention"
    __table_args__ = (
        Index("uq_run_intervention_source", "agent_run_id", "kind", "source_key", unique=True),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    #: ``review_changes_requested`` | ``foreign_commit``.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: What makes a redelivery the same intervention: the review id or the pushed head.
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
