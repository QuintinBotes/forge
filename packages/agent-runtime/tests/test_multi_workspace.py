"""F22 — multi-worktree workspace (AC 1: distinct worktrees, same branch; AC 8)."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from forge_agent.multi_repo import MultiRepoWorkspace
from forge_contracts import RepoTarget

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _init_repo(root: Path, *, branch: str = "main") -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", branch, str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"], check=True, capture_output=True
    )
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text("print('hi')\n")
    (root / "AGENTS.md").write_text("# rules\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "init"], check=True, capture_output=True
    )


@pytest.fixture
def two_repos(tmp_path: Path) -> dict[str, str]:
    api = tmp_path / "api"
    web = tmp_path / "web"
    _init_repo(api)
    _init_repo(web)
    return {"github.com/org/api": str(api), "github.com/org/web": str(web)}


def _targets() -> list[RepoTarget]:
    return [
        RepoTarget(repo="github.com/org/api", role="primary", base_branch="main"),
        RepoTarget(
            repo="github.com/org/web",
            role="secondary",
            base_branch="main",
            depends_on=["github.com/org/api"],
        ),
    ]


def test_create_all_distinct_worktrees_same_branch(two_repos: dict[str, str]) -> None:
    ws = MultiRepoWorkspace(two_repos, agent_run_id=uuid.uuid4())
    try:
        handles = ws.create_all(_targets(), branch_name="forge/TASK-401")
        assert set(handles) == {"github.com/org/api", "github.com/org/web"}
        api_path = handles["github.com/org/api"].worktree_path
        web_path = handles["github.com/org/web"].worktree_path
        assert api_path.exists() and web_path.exists()
        assert api_path != web_path
        # same branch across repos
        assert handles["github.com/org/api"].branch_name == "forge/TASK-401"
        assert handles["github.com/org/web"].branch_name == "forge/TASK-401"
        # base sha recorded
        assert len(handles["github.com/org/api"].base_commit_sha) >= 7
        assert handles["github.com/org/api"].role == "primary"
    finally:
        ws.cleanup_all()


def test_missing_mirror_fails_fast_named(two_repos: dict[str, str]) -> None:
    ws = MultiRepoWorkspace({"github.com/org/api": two_repos["github.com/org/api"]})
    with pytest.raises(KeyError, match=r"github\.com/org/web"):
        ws.create_all(_targets(), branch_name="forge/x")
    ws.cleanup_all()


def test_change_sets_has_changes_flag(two_repos: dict[str, str]) -> None:
    """AC 8: repo with edits => has_changes True; untouched => False."""
    ws = MultiRepoWorkspace(two_repos)
    try:
        handles = ws.create_all(_targets(), branch_name="forge/TASK-401")
        # Edit api only.
        (handles["github.com/org/api"].worktree_path / "app" / "main.py").write_text(
            "print('changed')\n"
        )
        ws.commit_all_repos("apply changes")
        change_sets = {cs.repo: cs for cs in ws.change_sets()}
        assert change_sets["github.com/org/api"].has_changes is True
        assert "app/main.py" in change_sets["github.com/org/api"].changed_files
        assert change_sets["github.com/org/web"].has_changes is False
        assert change_sets["github.com/org/web"].changed_files == []
    finally:
        ws.cleanup_all()


def test_cleanup_all_is_idempotent(two_repos: dict[str, str]) -> None:
    ws = MultiRepoWorkspace(two_repos)
    handles = ws.create_all(_targets(), branch_name="forge/x")
    paths = [h.worktree_path for h in handles.values()]
    ws.cleanup_all()
    ws.cleanup_all()  # no raise
    assert all(not p.exists() for p in paths)
