"""Minimal, local-only git helpers for the coordinator (no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["GitError", "git", "rev_parse"]


class GitError(RuntimeError):
    """A git command failed."""


def git(repo: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <repo> <args>`` locally and return the completed process."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git always present in CI
        raise GitError("git executable not found") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def rev_parse(repo: str | Path, ref: str = "HEAD") -> str:
    """Return the resolved sha of ``ref``."""
    return git(repo, "rev-parse", ref).stdout.strip()
