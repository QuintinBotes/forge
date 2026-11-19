"""The gates on a governed run's diff, beside its declared checks (#187).

``forge.run_checks`` judges the change from the merge base with the base branch to
the head commit: ``secret_scan`` and ``sast`` (``scf_policy``'s secret and SAST
gates). Each is one check, failed on a finding or when it could not verify, and
recorded with the run as the declared checks are. The secret scan reads every
commit and commit message, as the pull request carries them all. What the gates
allow comes from the default branch's policy, so a change cannot loosen its own
gates. They read the diff and the files; nothing from the change runs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from scf_contracts.dtos import DiffGateRules
from scf_policy.diff_gate import GateFinding, GateResult, added_lines, gate_result
from scf_policy.sast_gate import SAST_CHECK, SAST_SUFFIXES, run_sast
from scf_policy.secret_gate import SECRET_SCAN_CHECK, scan_secrets
from scf_worker.run_workspace import CheckRun

__all__ = ["diff_gate_checks"]

_GIT_TIMEOUT = 60.0


def _git(repo: Path, *args: str) -> str:
    """Git's output, with bytes that are not UTF-8 replaced: a binary file is content."""
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
    )
    return done.stdout.decode("utf-8", "replace")


def _at(repo: Path, ref: str, path: str) -> str | None:
    """``path`` at ``ref``, or ``None`` when it cannot be read (a gate cannot verify it)."""
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


#: --text: a .gitattributes in the change cannot hide a file's lines as binary.
_PATCH = ("--no-color", "--no-ext-diff", "--no-textconv", "--text", "-U0")


def _secret_scan(
    mirror: Path, fork: str, head: str, rules: DiffGateRules, masked: set[str]
) -> GateResult:
    """Each commit's added lines, message and added paths: the pull request pushes them all.

    A path that holds a credential is added to ``masked``, to be kept out of what is stored.
    """
    findings: list[GateFinding] = []
    allowed: list[GateFinding] = []
    for commit in _git(mirror, "rev-list", "--reverse", f"{fork}..{head}").split():
        note = {"message": f"in commit {commit[:12]}"}
        message = _git(mirror, "log", "-1", "--format=%B", commit).split("\n")
        listing = _git(
            mirror, "diff", "--name-only", "--diff-filter=ACR", "-z", f"{commit}^", commit
        )
        paths = [p for p in listing.split("\0") if p]
        in_paths = scan_secrets({"file path": list(enumerate(paths, start=1))})  # no allowance
        masked.update(paths[f.line - 1] for f in in_paths.findings)
        for result in (
            scan_secrets(
                added_lines(_git(mirror, "diff", *_PATCH, f"{commit}^", commit)),
                allowed_files=rules.secret_allowed_files,
            ),
            scan_secrets({"commit message": list(enumerate(message, start=1))}),  # no allowance
            in_paths,
        ):
            findings += [f.model_copy(update=note) for f in result.findings]
            allowed += [f.model_copy(update=note) for f in result.allowed]
    return gate_result(findings, allowed=allowed)


def _masked(text: str, paths: set[str]) -> str:
    for path in sorted(paths, key=len, reverse=True):
        text = text.replace(path, "<a path holding a credential>")
    return text


def diff_gate_checks(
    mirror: Path, *, base: str, head: str, rules: DiffGateRules
) -> tuple[list[CheckRun], str | None]:
    """Run the gates on ``base...head`` in ``mirror``, one check each.

    Also returns why to pause for a person: a gate the worker could not run (no
    analyser) is no fault of the builder's.
    """
    fork = _git(mirror, "merge-base", base, head).strip()
    listing = _git(mirror, "diff", "--raw", "--no-renames", "-z", fork, head).split("\0")
    # ":<old mode> <new mode> <old sha> <new sha> <status>", then the path
    raw = {path: header.split() for header, path in zip(listing[0::2], listing[1::2], strict=False)}
    # a symlink's blob is its target's name, and a type change is not the file it was
    regular = {p for p, h in raw.items() if h[1] in ("100644", "100755") and h[4] != "T"}
    masked: set[str] = set()
    gates: dict[str, Callable[[], GateResult]] = {
        SECRET_SCAN_CHECK: lambda: _secret_scan(mirror, fork, head, rules, masked),
        SAST_CHECK: lambda: run_sast(
            {
                p: _at(mirror, head, p) if p in regular else None
                for p, h in raw.items()
                if h[4] != "D" and p.endswith(SAST_SUFFIXES)
            },
            analyzer=rules.sast_analyzer,
            severity=rules.sast_severity,
        ),
    }
    checks = []
    pause = None
    for name, gate in gates.items():
        started_at = datetime.now(UTC)
        result = gate()
        checks.append(
            CheckRun(
                id=name,
                exit_code=0 if result.passed else 1,
                timed_out=False,
                started_at=started_at,
                duration_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
                output_tail=_masked(result.report(), masked),
            )
        )
        if result.unavailable and pause is None:
            pause = _masked(f"{name} could not verify: {'; '.join(result.unavailable)}", masked)
    return checks, pause
