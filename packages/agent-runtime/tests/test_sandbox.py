"""Unit tests for the git-worktree sandbox and AGENTS.md loader."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from forge_agent.sandbox import WorktreeSandbox, load_agents_md

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (root / "AGENTS.md").write_text("# Repo rules\nAlways write tests first.\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "init"], check=True, capture_output=True
    )


def test_load_agents_md_reads_file(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("hello agents")
    assert load_agents_md(tmp_path) == "hello agents"


def test_load_agents_md_missing_returns_none(tmp_path: Path) -> None:
    assert load_agents_md(tmp_path) is None


def test_worktree_created_and_cleaned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    sandbox = WorktreeSandbox(repo, base_branch="main")
    worktree = sandbox.create("forge/test-1")
    assert worktree.exists()
    assert (worktree / "AGENTS.md").exists()
    assert load_agents_md(worktree) is not None

    sandbox.cleanup()
    assert not worktree.exists()


def test_worktree_context_manager(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    with WorktreeSandbox(repo, base_branch="main") as worktree:
        assert worktree.exists()
        path = worktree
    assert not path.exists()
