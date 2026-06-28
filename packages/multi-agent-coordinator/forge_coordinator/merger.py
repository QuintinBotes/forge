"""Deterministic git merge of subagent branches onto integration (F27 §4).

``BranchMerger`` NEVER auto-resolves a conflict. The merge is **all-or-nothing**:
if any code-producing branch conflicts, every partial merge is discarded (the
integration branch is reset to its base sha) and the conflicts are returned for a
human ``interrupt`` — no silent overwrite, no partial commit.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from forge_contracts import (
    CODE_PRODUCING_ROLES,
    MergeConflict,
    MergeResult,
    SubAgentResult,
)
from forge_coordinator.gitutil import git, rev_parse

__all__ = ["BranchMerger"]


class BranchMerger:
    """Merge code-producing subagent branches onto the integration branch."""

    def merge(
        self,
        *,
        repo: str | Path,
        integration_branch: str,
        results: list[SubAgentResult],
        base_sha: str,
        strategy: str = "sequential_integration",
    ) -> MergeResult:
        repo = Path(repo)
        code_results = [
            r
            for r in results
            if r.role in CODE_PRODUCING_ROLES
            and r.status == "succeeded"
            and r.artifact.branch_name
        ]
        if strategy == "read_only" or not code_results:
            head = rev_parse(repo, integration_branch)
            return MergeResult(
                integration_branch=integration_branch,
                head_sha=head,
                merged_assignments=[],
                conflicts=[],
                changed_files=[],
                diff_stat={"files": 0, "insertions": 0, "deletions": 0},
            )

        holder = Path(tempfile.mkdtemp(prefix="forge-merge-"))
        work = holder / "tree"
        git(repo, "worktree", "add", "--force", str(work), integration_branch)
        try:
            git(work, "reset", "--hard", base_sha, check=False)
            merged: list[str] = []
            conflicts: list[MergeConflict] = []
            for result in code_results:
                branch = result.artifact.branch_name
                assert branch is not None
                proc = git(
                    work,
                    "merge",
                    "--no-ff",
                    "-m",
                    f"merge {result.assignment_id}",
                    branch,
                    check=False,
                )
                if proc.returncode != 0:
                    conflicted = git(
                        work,
                        "diff",
                        "--name-only",
                        "--diff-filter=U",
                        check=False,
                    ).stdout.split()
                    if not conflicted:
                        conflicted = [""]
                    conflicts.extend(
                        MergeConflict(
                            assignment_id=result.assignment_id,
                            path=path,
                            detail="merge conflict; manual resolution required",
                        )
                        for path in conflicted
                    )
                    git(work, "merge", "--abort", check=False)
                    break
                merged.append(result.assignment_id)

            if conflicts:
                # All-or-nothing: discard every partial merge (no silent overwrite).
                # The worktree has ``integration_branch`` checked out, so the reset
                # moves the branch ref itself back to base.
                git(work, "reset", "--hard", base_sha, check=False)
                return MergeResult(
                    integration_branch=integration_branch,
                    head_sha=base_sha,
                    merged_assignments=[],
                    conflicts=conflicts,
                    changed_files=[],
                    diff_stat={"files": 0, "insertions": 0, "deletions": 0},
                )

            # The merge commits landed on ``integration_branch`` (checked out in the
            # worktree), so its ref already points at the merged HEAD.
            head = rev_parse(work, "HEAD")
            changed = [
                p
                for p in git(
                    work, "diff", "--name-only", f"{base_sha}", "HEAD", check=False
                ).stdout.split("\n")
                if p
            ]
            diff_stat = _numstat(work, base_sha)
            return MergeResult(
                integration_branch=integration_branch,
                head_sha=head,
                merged_assignments=merged,
                conflicts=[],
                changed_files=changed,
                diff_stat=diff_stat,
            )
        finally:
            git(repo, "worktree", "remove", "--force", str(work), check=False)
            shutil.rmtree(holder, ignore_errors=True)
            git(repo, "worktree", "prune", check=False)


def _numstat(work: str | Path, base_sha: str) -> dict[str, int]:
    out = git(work, "diff", "--numstat", base_sha, "HEAD", check=False).stdout
    files = insertions = deletions = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        files += 1
        add, dele, _ = parts
        if add.isdigit():
            insertions += int(add)
        if dele.isdigit():
            deletions += int(dele)
    return {"files": files, "insertions": insertions, "deletions": deletions}
