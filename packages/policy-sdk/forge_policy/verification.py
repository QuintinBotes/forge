"""Automatic static-gate verification over produced files (F40-POL-GOVERNANCE).

The scanner in :mod:`forge_policy.static_gate` is content-in / violations-out. This
module is its *pipeline* half — the verification-service seam that turns a skill
profile's ``forbidden_shortcuts`` from a mere prompt directive into a real check.
It collects the text a run actually produced (a directory of files, e.g. an
agent's git worktree) and runs the scan, folding the outcome into the run's named
check set (``{"forbidden_shortcuts": bool}``) alongside lint/type/test/coverage.
A run that took a banned shortcut therefore **fails verification** automatically,
not just at the on-demand ``POST /policy/static-gate`` surface.

Collection is pure filesystem I/O (no network); the scan itself stays total and
non-Turing (literal substring matching), so the gate can never hang a pipeline.
"""

from __future__ import annotations

from pathlib import Path

from forge_policy.static_gate import StaticGateResult, scan_forbidden_shortcuts

__all__ = [
    "STATIC_GATE_CHECK",
    "collect_files",
    "run_static_gate",
    "static_gate_check",
]

#: The named check the static gate contributes to a run's verification result.
STATIC_GATE_CHECK = "forbidden_shortcuts"

#: Directories never descended when collecting produced files.
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
        ".pytest_cache",
    }
)


def collect_files(
    root: str | Path,
    *,
    paths: list[str] | None = None,
) -> dict[str, str]:
    """Read the produced text files under ``root`` into ``{relpath: text}``.

    With ``paths`` only those repo-relative files are read (the change set an
    agent produced); without it the whole tree is walked, skipping VCS/build
    directories. Binary/undecodable files are silently skipped so the scan only
    sees source text.
    """
    base = Path(root)
    if paths is not None:
        candidates = [base / p for p in paths]
    else:
        candidates = [
            p
            for p in base.rglob("*")
            if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(base).parts)
        ]
    files: dict[str, str] = {}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files[path.relative_to(base).as_posix()] = text
    return files


def run_static_gate(
    root: str | Path,
    forbidden_shortcuts: list[str],
    *,
    paths: list[str] | None = None,
) -> StaticGateResult:
    """Scan a produced worktree for ``forbidden_shortcuts``; a hit fails the gate."""
    files = collect_files(root, paths=paths)
    return scan_forbidden_shortcuts(files, forbidden_shortcuts)


def static_gate_check(result: StaticGateResult) -> dict[str, bool]:
    """Fold a gate result into the named check map the verification step reports."""
    return {STATIC_GATE_CHECK: result.passed}
