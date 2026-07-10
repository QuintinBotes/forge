"""Unit tests for the git-worktree sandbox and AGENTS.md loader."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from forge_agent.sandbox import (
    SandboxError,
    WorktreeSandbox,
    _git,
    discover_agents_md,
    load_agents_md,
)

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


def test_load_agents_md_merges_nested_files(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rules")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "AGENTS.md").write_text("app rules")
    merged = load_agents_md(tmp_path)
    assert merged is not None
    assert "# AGENTS.md (AGENTS.md)\nroot rules" in merged
    assert "# AGENTS.md (app/AGENTS.md)\napp rules" in merged
    # Root section precedes nested sections.
    assert merged.index("root rules") < merged.index("app rules")


def test_load_agents_md_skips_noise_dirs(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("only root")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "AGENTS.md").write_text("vendored noise")
    # A lone root file (noise dir skipped) is returned verbatim, no header.
    assert load_agents_md(tmp_path) == "only root"


def test_discover_agents_md_subpath_scopes_to_ancestry(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root")
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "AGENTS.md").write_text("a")
    (tmp_path / "a" / "b" / "AGENTS.md").write_text("b")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "AGENTS.md").write_text("other")
    found = discover_agents_md(tmp_path, subpath="a/b")
    rels = [p.relative_to(tmp_path).as_posix() for p in found]
    assert rels == ["AGENTS.md", "a/AGENTS.md", "a/b/AGENTS.md"]


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


def test_create_twice_raises_already_created(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    sandbox = WorktreeSandbox(repo, base_branch="main")
    sandbox.create("forge/once")
    try:
        with pytest.raises(SandboxError, match="already created"):
            sandbox.create("forge/twice")
    finally:
        sandbox.cleanup()


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    sandbox = WorktreeSandbox(repo, base_branch="main")
    worktree = sandbox.create("forge/clean")
    sandbox.cleanup()
    # A second cleanup on an already-removed sandbox is a no-op (no raise).
    sandbox.cleanup()
    assert not worktree.exists()


def test_git_missing_binary_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr("forge_agent.sandbox.subprocess.run", _raise)
    with pytest.raises(SandboxError, match="git executable not found"):
        _git(tmp_path, "status")


def test_git_command_failure_raises(tmp_path: Path) -> None:
    # ``tmp_path`` is not a git repository, so ``git status`` exits non-zero and
    # the helper surfaces a SandboxError carrying git's stderr.
    with pytest.raises(SandboxError, match="failed"):
        _git(tmp_path, "status")


# --------------------------------------------------------------------------- #
# static forbidden-shortcuts verification (F40-POL-GOVERNANCE)                 #
# --------------------------------------------------------------------------- #

_FORBIDDEN = ["# type: ignore", "skip failing tests"]


def test_verify_forbidden_shortcuts_passes_on_clean_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sandbox = WorktreeSandbox(repo, base_branch="main")
    worktree = sandbox.create("forge/clean")
    (worktree / "feature.py").write_text("def f() -> int:\n    return 1\n")
    try:
        result = sandbox.verify_forbidden_shortcuts(_FORBIDDEN)
        assert result.passed is True
    finally:
        sandbox.cleanup()


def test_verify_forbidden_shortcuts_fails_on_banned_shortcut(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sandbox = WorktreeSandbox(repo, base_branch="main")
    worktree = sandbox.create("forge/dirty")
    (worktree / "feature.py").write_text("x = 1  # type: ignore\n")
    try:
        result = sandbox.verify_forbidden_shortcuts(_FORBIDDEN)
        assert result.passed is False
        assert result.violations[0].file == "feature.py"
    finally:
        sandbox.cleanup()


def test_verify_forbidden_shortcuts_only_scans_change_set(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # A pre-existing committed violation on main must NOT fail a clean change.
    (repo / "legacy.py").write_text("y = 2  # type: ignore\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "legacy"], check=True, capture_output=True
    )
    sandbox = WorktreeSandbox(repo, base_branch="main")
    worktree = sandbox.create("forge/scoped")
    (worktree / "feature.py").write_text("ok = True\n")
    try:
        assert sandbox.verify_forbidden_shortcuts(_FORBIDDEN).passed is True
    finally:
        sandbox.cleanup()


def test_verify_forbidden_shortcuts_requires_created_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sandbox = WorktreeSandbox(repo, base_branch="main")
    with pytest.raises(SandboxError, match="not created"):
        sandbox.verify_forbidden_shortcuts(_FORBIDDEN)
