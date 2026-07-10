"""Git-worktree sandbox and ``AGENTS.md`` loader (V1 isolation).

V1 isolation uses git worktrees (spec: "Sandbox (V1) | Git worktrees"). A
:class:`WorktreeSandbox` creates a throwaway worktree+branch off a base ref and
removes it on cleanup, giving an agent an isolated checkout with no cross-task
filesystem access. All git operations are local (no network).

This module is unchanged from F06 except that :class:`SandboxError` now lives in
``forge_agent.sandbox.base`` so the worktree and container error families share a
single root; the public ``forge_agent.sandbox.SandboxError`` name is preserved.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from types import TracebackType

from forge_agent.sandbox.base import SandboxError
from forge_policy.static_gate import StaticGateResult
from forge_policy.verification import run_static_gate

__all__ = ["SandboxError", "WorktreeSandbox", "discover_agents_md", "load_agents_md"]

#: Candidate AGENTS.md filenames, in priority order.
_AGENTS_FILENAMES = ("AGENTS.md", "agents.md", "AGENTS.MD")

#: Directories never descended when discovering nested AGENTS.md files.
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".next",
        "dist",
        "build",
        ".mypy_cache",
    }
)


def _agents_md_in(directory: Path) -> Path | None:
    """Return the AGENTS.md file directly in ``directory`` (first name wins)."""
    for name in _AGENTS_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def discover_agents_md(repo_root: str | Path, *, subpath: str | Path | None = None) -> list[Path]:
    """Discover AGENTS.md files to merge, ordered root-first then by depth/name.

    Without ``subpath`` every AGENTS.md in the tree is returned (root plus each
    nested one). With ``subpath`` only the files on the root→subpath ancestry are
    returned (the hierarchical scope an agent working in that subdir sees), so a
    deep worktree merges its own directory chain, not the whole repo.
    """
    root = Path(repo_root)
    if subpath is not None:
        found: list[Path] = []
        current = root
        candidate = _agents_md_in(current)
        if candidate is not None:
            found.append(candidate)
        for part in Path(subpath).parts:
            current = current / part
            if not current.is_dir():
                break
            nested = _agents_md_in(current)
            if nested is not None:
                found.append(nested)
        return found

    root_file = _agents_md_in(root)
    nested_files: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        directory = Path(dirpath)
        if directory == root:
            continue
        nested = _agents_md_in(directory)
        if nested is not None:
            nested_files.append(nested)
    nested_files.sort(key=lambda p: (len(p.relative_to(root).parts), p.as_posix()))
    return ([root_file] if root_file is not None else []) + nested_files


def load_agents_md(repo_root: str | Path, *, subpath: str | Path | None = None) -> str | None:
    """Return the merged ``AGENTS.md`` narrative for ``repo_root``, or ``None``.

    Spec: ``AGENTS.md`` is "narrative instructions loaded into agent context at
    run time". A repo may place additional ``AGENTS.md`` files in subdirectories
    to add path-scoped instructions; this loader merges them (root first) under
    per-file headers. A lone root file is returned verbatim (no header) so the
    single-file case is byte-for-byte unchanged.
    """
    root = Path(repo_root)
    files = discover_agents_md(root, subpath=subpath)
    if not files:
        return None
    if len(files) == 1 and files[0].parent == root:
        return files[0].read_text()

    sections: list[str] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        sections.append(f"# AGENTS.md ({rel})\n{path.read_text().strip()}")
    return "\n\n".join(sections)


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

    def _changed_files(self) -> list[str]:
        """Repo-relative paths this run changed vs base (tracked diff + untracked)."""
        tree = self.worktree_path
        assert tree is not None  # guarded by callers
        tracked = _git(tree, "diff", "--name-only", self.base_branch).stdout.splitlines()
        untracked = _git(tree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
        seen = dict.fromkeys(p for p in (*tracked, *untracked) if p.strip())
        return list(seen)

    def verify_forbidden_shortcuts(self, forbidden_shortcuts: list[str]) -> StaticGateResult:
        """Static forbidden-shortcuts verification over this run's change set.

        A real verification check (not just a prompt directive): scans the files
        the run actually produced in this worktree and returns a *failing*
        :class:`StaticGateResult` when any forbidden shortcut is present, so the
        verification step can gate the run rather than merely asking the agent to
        avoid the shortcut. Only changed/added files are scanned, keeping the gate
        scoped to the run's own output.
        """
        if self.worktree_path is None:
            raise SandboxError("sandbox not created")
        return run_static_gate(self.worktree_path, forbidden_shortcuts, paths=self._changed_files())

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
