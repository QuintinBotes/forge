"""Integration branch + per-subagent worktree management."""

from __future__ import annotations

from pathlib import Path

from forge_coordinator import SubAgentWorkspaceManager
from forge_coordinator.gitutil import git, rev_parse


def test_integration_branch_created_off_base(tmp_git_repo: Path) -> None:
    ws = SubAgentWorkspaceManager(tmp_git_repo)
    base = ws.ensure_integration_branch(base_branch="main", integration_branch="forge/int")
    assert rev_parse(tmp_git_repo, "forge/int") == base


def test_subagent_worktrees_are_distinct(tmp_git_repo: Path) -> None:
    ws = SubAgentWorkspaceManager(tmp_git_repo)
    ws.ensure_integration_branch(base_branch="main", integration_branch="forge/int")
    h1 = ws.create_subagent_branch(integration_branch="forge/int", assignment_id="sa-implementer-1")
    h2 = ws.create_subagent_branch(integration_branch="forge/int", assignment_id="sa-implementer-2")
    assert h1.worktree_path != h2.worktree_path
    assert h1.branch_name != h2.branch_name
    assert h1.worktree_path.is_dir()
    assert (h1.worktree_path / "app" / "__init__.py").is_file()


def test_cleanup_keeps_branches(tmp_git_repo: Path) -> None:
    ws = SubAgentWorkspaceManager(tmp_git_repo)
    ws.ensure_integration_branch(base_branch="main", integration_branch="forge/int")
    h1 = ws.create_subagent_branch(integration_branch="forge/int", assignment_id="sa-implementer-1")
    branch = h1.branch_name
    ws.cleanup(keep_branches=True)
    assert not h1.worktree_path.exists()
    # Branch ref still resolvable.
    assert git(tmp_git_repo, "rev-parse", "--verify", branch, check=False).returncode == 0
