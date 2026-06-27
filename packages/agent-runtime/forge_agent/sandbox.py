"""Git-worktree sandbox and ``AGENTS.md`` loader.

V1 isolation uses git worktrees (spec: "Sandbox (V1) | Git worktrees"). A
:class:`WorktreeSandbox` creates a throwaway worktree+branch off a base ref and
removes it on cleanup, giving an agent an isolated checkout with no cross-task
filesystem access. All git operations are local (no network).
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from types import TracebackType

__all__ = ["SandboxError", "WorktreeSandbox", "load_agents_md"]

#: Candidate AGENTS.md filenames, in priority order.
_AGENTS_FILENAMES = ("AGENTS.md", "agents.md", "AGENTS.MD")


class SandboxError(RuntimeError):
    """Raised when a worktree operation fails."""


def load_agents_md(repo_root: str | Path) -> str | None:
    """Return the text of ``AGENTS.md`` at ``repo_root``, or ``None`` if absent.

    Spec: ``AGENTS.md`` is "narrative instructions loaded into agent context at
    run time".
    """
    root = Path(repo_root)
    for name in _AGENTS_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate.read_text()
    return None


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # git binary missing
        raise SandboxError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        raise SandboxError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc


class WorktreeSandbox:
    """An isolated git worktree for a single agent run.

    Usage::

        with WorktreeSandbox(repo, base_branch="main") as worktree:
            ...  # work inside `worktree`
        # worktree is removed here

    Or imperatively via :meth:`create` / :meth:`cleanup`.
    """

    def __init__(
        self,
        repo_root: str | Path,
        *,
        base_branch: str = "main",
        branch_prefix: str = "forge",
    ) -> None:
        self.repo_root = Path(repo_root)
        self.base_branch = base_branch
        self.branch_prefix = branch_prefix
        self._holder: Path | None = None
        self.worktree_path: Path | None = None
        self.branch: str | None = None

    def create(self, branch: str | None = None) -> Path:
        """Create the worktree+branch and return its path."""
        if self.worktree_path is not None:
            raise SandboxError("sandbox already created")
        self.branch = branch or f"{self.branch_prefix}/{uuid.uuid4().hex[:8]}"
        self._holder = Path(tempfile.mkdtemp(prefix="forge-wt-"))
        target = self._holder / "tree"
        _git(
            self.repo_root,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(target),
            self.base_branch,
        )
        self.worktree_path = target
        return target

    def cleanup(self) -> None:
        """Remove the worktree and its temp holder. Safe to call repeatedly."""
        if self.worktree_path is not None:
            # On failure, fall back to filesystem removal of the holder below.
            with contextlib.suppress(SandboxError):
                _git(self.repo_root, "worktree", "remove", "--force", str(self.worktree_path))
            self.worktree_path = None
        if self._holder is not None:
            shutil.rmtree(self._holder, ignore_errors=True)
            self._holder = None
        # Drop any now-stale worktree administrative entries.
        with contextlib.suppress(SandboxError):
            _git(self.repo_root, "worktree", "prune")

    def __enter__(self) -> Path:
        return self.create()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.cleanup()
