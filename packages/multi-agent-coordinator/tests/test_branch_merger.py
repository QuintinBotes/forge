"""Deterministic branch merge + conflict detection (AC 8, 9)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from forge_contracts import SubAgentArtifact, SubAgentResult, SubAgentRole
from forge_coordinator import BranchMerger, SubAgentWorkspaceManager
from forge_coordinator.gitutil import rev_parse


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _impl_result(assignment_id: str, branch: str) -> SubAgentResult:
    return SubAgentResult(
        assignment_id=assignment_id,
        role=SubAgentRole.IMPLEMENTER,
        status="succeeded",
        confidence=0.9,
        artifact=SubAgentArtifact(kind="code_change", summary="impl", branch_name=branch),
    )


def _make_branch(ws: SubAgentWorkspaceManager, integration: str, aid: str, rel: str, content: str):
    handle = ws.create_subagent_branch(integration_branch=integration, assignment_id=aid)
    target = handle.worktree_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(handle.worktree_path, "add", "-A")
    _git(handle.worktree_path, "commit", "-m", f"{aid}")
    return handle


def test_disjoint_files_merge_clean(tmp_git_repo: Path) -> None:
    ws = SubAgentWorkspaceManager(tmp_git_repo)
    base = ws.ensure_integration_branch(base_branch="main", integration_branch="forge/int")
    h1 = _make_branch(ws, "forge/int", "sa-implementer-1", "app/a.py", "A=1\n")
    h2 = _make_branch(ws, "forge/int", "sa-implementer-2", "app/b.py", "B=2\n")

    result = BranchMerger().merge(
        repo=tmp_git_repo,
        integration_branch="forge/int",
        results=[
            _impl_result("sa-implementer-1", h1.branch_name),
            _impl_result("sa-implementer-2", h2.branch_name),
        ],
        base_sha=base,
        strategy="fan_in_merge",
    )
    assert result.conflicts == []
    assert set(result.merged_assignments) == {"sa-implementer-1", "sa-implementer-2"}
    assert "app/a.py" in result.changed_files
    assert "app/b.py" in result.changed_files
    # Integration branch advanced past base.
    assert rev_parse(tmp_git_repo, "forge/int") != base
    ws.cleanup()


def test_same_line_edits_conflict_and_nothing_committed(tmp_git_repo: Path) -> None:
    ws = SubAgentWorkspaceManager(tmp_git_repo)
    base = ws.ensure_integration_branch(base_branch="main", integration_branch="forge/int")
    h1 = _make_branch(ws, "forge/int", "sa-implementer-1", "app/__init__.py", "VALUE = 'one'\n")
    h2 = _make_branch(ws, "forge/int", "sa-implementer-2", "app/__init__.py", "VALUE = 'two'\n")

    result = BranchMerger().merge(
        repo=tmp_git_repo,
        integration_branch="forge/int",
        results=[
            _impl_result("sa-implementer-1", h1.branch_name),
            _impl_result("sa-implementer-2", h2.branch_name),
        ],
        base_sha=base,
        strategy="fan_in_merge",
    )
    assert result.conflicts, "expected a merge conflict"
    assert result.merged_assignments == []
    # All-or-nothing: integration branch was reset to base; nothing committed.
    assert rev_parse(tmp_git_repo, "forge/int") == base
    ws.cleanup()


def test_read_only_results_produce_no_merge(tmp_git_repo: Path) -> None:
    ws = SubAgentWorkspaceManager(tmp_git_repo)
    base = ws.ensure_integration_branch(base_branch="main", integration_branch="forge/int")
    reviewer = SubAgentResult(
        assignment_id="sa-reviewer-1",
        role=SubAgentRole.REVIEWER,
        status="succeeded",
        confidence=0.9,
        artifact=SubAgentArtifact(kind="review", summary="ok", review_verdict="approved"),
    )
    result = BranchMerger().merge(
        repo=tmp_git_repo,
        integration_branch="forge/int",
        results=[reviewer],
        base_sha=base,
        strategy="read_only",
    )
    assert result.merged_assignments == []
    assert result.conflicts == []
