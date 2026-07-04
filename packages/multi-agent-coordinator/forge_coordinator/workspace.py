"""Per-subagent worktrees + the shared integration branch (F27 §4).

Each subagent gets its **own** git worktree off the integration branch (blast-
radius control). The integration branch is created off the task's base branch and
is the only branch the coordinator advances; merging it to a base branch stays
gated by F08's human PR approval.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from forge_coordinator.gitutil import git, rev_parse

__all__ = ["SubAgentWorkspaceManager", "WorktreeHandle"]


@dataclass
class WorktreeHandle:
    """A single subagent's worktree + branch."""

    assignment_id: str
    worktree_path: Path
    branch_name: str
    base_branch: str
    base_sha: str
    holder: Path = field(repr=False, default_factory=Path)


class SubAgentWorkspaceManager:
    """Creates per-subagent worktrees off the integration branch."""

    def __init__(self, repo: str | Path, *, branch_prefix: str = "forge") -> None:
        self.repo = Path(repo)
        self.branch_prefix = branch_prefix
        self._handles: list[WorktreeHandle] = []
        self.integration_branch: str | None = None
        self.integration_base_sha: str | None = None

    def ensure_integration_branch(
        self, *, base_branch: str, integration_branch: str
    ) -> str:
        """Create (or reset) ``integration_branch`` at ``base_branch`` HEAD.

        Returns the base sha. The integration branch is **not** checked out in the
        main repo (so a merge worktree may check it out later).
        """
        base_sha = rev_parse(self.repo, base_branch)
        existing = git(
            self.repo, "rev-parse", "--verify", "--quiet", integration_branch, check=False
        )
        if existing.returncode == 0:
            git(self.repo, "branch", "-f", integration_branch, base_branch)
        else:
            git(self.repo, "branch", integration_branch, base_branch)
        self.integration_branch = integration_branch
        self.integration_base_sha = base_sha
        return base_sha

    def create_subagent_branch(
        self, *, integration_branch: str, assignment_id: str
    ) -> WorktreeHandle:
        """Create a fresh worktree + branch off the integration branch HEAD."""
        base_sha = rev_parse(self.repo, integration_branch)
        branch = f"{self.branch_prefix}/{assignment_id}-{uuid.uuid4().hex[:6]}"
        holder = Path(tempfile.mkdtemp(prefix="forge-sa-"))
        target = holder / "tree"
        git(self.repo, "worktree", "add", "-b", branch, str(target), integration_branch)
        handle = WorktreeHandle(
            assignment_id=assignment_id,
            worktree_path=target,
            branch_name=branch,
            base_branch=integration_branch,
            base_sha=base_sha,
            holder=holder,
        )
        self._handles.append(handle)
        return handle

    def cleanup(self, *, keep_branches: bool = True) -> None:
        """Remove all subagent worktrees; keep the branches by default."""
        for handle in self._handles:
            git(
                self.repo,
                "worktree",
                "remove",
                "--force",
                str(handle.worktree_path),
                check=False,
            )
            shutil.rmtree(handle.holder, ignore_errors=True)
            if not keep_branches:
                git(self.repo, "branch", "-D", handle.branch_name, check=False)
        git(self.repo, "worktree", "prune", check=False)
        self._handles.clear()
