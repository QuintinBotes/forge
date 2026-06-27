"""F22 multi-repo execution models: PRGroup + AgentRepoWorkspace.

These extend the existing run/agent tables (``workflow_run``, ``agent_run``)
rather than introduce a parallel model graph:

* :class:`PRGroup` — one row per multi-repo run's PR set (the merge unit). It
  records the topological ``merge_order`` and, as each PR merges, the
  ``merged_repo_ids`` (the partial-merge audit trail).
* :class:`AgentRepoWorkspace` — one row per ``(agent_run, repo)`` worktree, the
  multi-worktree analogue of F06's single ``agent_run.worktree_path`` columns.

Deviations from the slice's idealised schema (conforming to the real foundation,
per the F22 brief):

* F08's per-run ``verification_report`` / ``pull_request`` tables do not exist in
  this foundation yet, so :class:`PRGroup` is self-contained (it does not FK to a
  ``pull_request`` row); per-repo PR identity is carried in the application-layer
  ``PRGroup`` DTO until F08's tables land.
* ``repo_policy_snapshot`` (F04) is likewise absent, so
  ``AgentRepoWorkspace.policy_snapshot_id`` is a nullable UUID without an FK.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import PRGroupStatus, RepoRole


class PRGroup(WorkspaceScopedModel):
    """The set of PRs opened by one multi-repo run — the merge unit (F22)."""

    __tablename__ = "pr_group"
    __table_args__ = (UniqueConstraint("workflow_run_id", name="uq_pr_group_workflow_run_id"),)

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
    )
    repo_count: Mapped[int] = mapped_column(default=0, nullable=False)
    merge_order: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    status: Mapped[PRGroupStatus] = mapped_column(
        enum_type(PRGroupStatus), default=PRGroupStatus.OPEN, nullable=False
    )
    merged_repo_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)


class AgentRepoWorkspace(WorkspaceScopedModel):
    """One ``(agent_run, repo)`` worktree record for a multi-repo run (F22)."""

    __tablename__ = "agent_repo_workspace"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id", "repo_id", name="uq_agent_repo_workspace_agent_run_id_repo_id"
        ),
    )

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    repo_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[RepoRole] = mapped_column(
        enum_type(RepoRole), default=RepoRole.SECONDARY, nullable=False
    )
    worktree_path: Mapped[str] = mapped_column(Text, nullable=False)
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # F04 repo_policy_snapshot table is absent in this foundation; nullable, no FK.
    policy_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


__all__ = ["AgentRepoWorkspace", "PRGroup"]
