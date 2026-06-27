"""Multi-worktree workspace for a single agent run spanning N repos (F22).

One :class:`AgentRun` operates over several repos at once (e.g. an API contract
change plus its generated client). Each repo gets its **own** isolated git
worktree under one run — the multi-worktree analogue of F06's single
:class:`WorktreeSandbox` — so a tool scoped to repo A structurally cannot touch
repo B's files (cross-repo isolation within a task; see
:class:`~forge_agent.policy_guard.MultiRepoPolicyGuard`).

All git operations are local (no network); the single-repo path is unchanged.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from forge_agent.sandbox import SandboxError, WorktreeSandbox, _git
from forge_contracts import RepoChangeSet, RepoTarget

__all__ = ["MultiRepoWorkspace", "WorktreeHandle"]


@dataclass
class WorktreeHandle:
    """A single repo's worktree within a multi-repo run."""

    repo_id: str
    role: str
    worktree_path: Path
    branch_name: str
    base_branch: str
    base_commit_sha: str
    head_commit_sha: str | None = None
    _sandbox: WorktreeSandbox | None = field(default=None, repr=False)


def _rev_parse(path: Path, ref: str = "HEAD") -> str:
    return _git(path, "rev-parse", ref).stdout.strip()


def _changed_files(worktree: Path, base_sha: str) -> list[str]:
    """Union of committed (base..HEAD) and working-tree (porcelain) changes."""
    files: set[str] = set()
    committed = _git(worktree, "diff", "--name-only", f"{base_sha}..HEAD").stdout
    files.update(line for line in committed.splitlines() if line.strip())
    porcelain = _git(worktree, "status", "--porcelain").stdout
    for line in porcelain.splitlines():
        name = line[3:].strip() if len(line) > 3 else ""
        if " -> " in name:  # rename: keep the destination path
            name = name.split(" -> ", 1)[1]
        if name:
            files.add(name)
    return sorted(files)


def _diff_stat(worktree: Path, base_sha: str) -> dict[str, int]:
    insertions = deletions = 0
    numstat = _git(worktree, "diff", "--numstat", f"{base_sha}..HEAD").stdout
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            insertions += int(parts[0])
            deletions += int(parts[1])
    return {"insertions": insertions, "deletions": deletions}


class MultiRepoWorkspace:
    """Create + manage one isolated worktree per repo target for an agent run."""

    def __init__(
        self,
        repo_roots: dict[str, str | Path],
        *,
        agent_run_id: uuid.UUID | None = None,
    ) -> None:
        #: ``repo_id -> local git repo (mirror/clone)`` the worktree branches off.
        self._repo_roots = {k: Path(v) for k, v in repo_roots.items()}
        self.agent_run_id = agent_run_id or uuid.uuid4()
        self._handles: dict[str, WorktreeHandle] = {}

    @property
    def repo_ids(self) -> list[str]:
        return list(self._handles)

    def create_all(
        self,
        repo_targets: list[RepoTarget],
        *,
        branch_name: str,
    ) -> dict[str, WorktreeHandle]:
        """Create one worktree per target, all on the same ``branch_name``.

        Each worktree branches off *its own* ``base_branch`` of *its own* repo;
        the branch name is shared across repos so the PRs line up (``forge/...``).
        Fails fast (named repo) if a target repo has no mirror under the run.
        """
        for target in repo_targets:
            if target.repo not in self._repo_roots:
                raise KeyError(f"repo target is not mirrored for this run: {target.repo!r}")
            if target.repo in self._handles:
                continue
            sandbox = WorktreeSandbox(self._repo_roots[target.repo], base_branch=target.base_branch)
            path = sandbox.create(branch_name)
            handle = WorktreeHandle(
                repo_id=target.repo,
                role=target.role,
                worktree_path=path,
                branch_name=branch_name,
                base_branch=target.base_branch,
                base_commit_sha=_rev_parse(path),
                _sandbox=sandbox,
            )
            self._handles[target.repo] = handle
        return dict(self._handles)

    def handle(self, repo_id: str) -> WorktreeHandle:
        try:
            return self._handles[repo_id]
        except KeyError as exc:
            raise KeyError(f"no worktree for repo {repo_id!r}") from exc

    def commit_all_repos(self, message: str) -> dict[str, str]:
        """Stage + commit pending changes in every worktree; return head shas.

        Repos with no pending change are left at their base commit (no empty
        commit); their head sha equals their base sha.
        """
        heads: dict[str, str] = {}
        for repo_id, handle in self._handles.items():
            path = handle.worktree_path
            _git(path, "add", "-A")
            status = _git(path, "status", "--porcelain").stdout.strip()
            if status:
                _git(path, "commit", "-m", message)
            head = _rev_parse(path)
            handle.head_commit_sha = head
            heads[repo_id] = head
        return heads

    def change_sets(self) -> list[RepoChangeSet]:
        """One :class:`RepoChangeSet` per repo (``has_changes`` computed)."""
        result: list[RepoChangeSet] = []
        for handle in self._handles.values():
            files = _changed_files(handle.worktree_path, handle.base_commit_sha)
            stat = _diff_stat(handle.worktree_path, handle.base_commit_sha)
            result.append(
                RepoChangeSet(
                    repo=handle.repo_id,
                    branch_name=handle.branch_name,
                    base_commit_sha=handle.base_commit_sha,
                    head_commit_sha=handle.head_commit_sha,
                    changed_files=files,
                    diff_stat={"files": len(files), **stat},
                    has_changes=bool(files),
                )
            )
        return result

    def cleanup_all(self, *, keep_branches: bool = True) -> None:
        """Remove every worktree (terminal-state cleanup). Idempotent."""
        for handle in self._handles.values():
            if handle._sandbox is not None:
                handle._sandbox.cleanup()
            if not keep_branches:
                root = self._repo_roots.get(handle.repo_id)
                if root is not None:
                    # Branch may already be gone / checked out elsewhere.
                    with contextlib.suppress(SandboxError):
                        _git(root, "branch", "-D", handle.branch_name)
        self._handles.clear()
