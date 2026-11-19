"""The gates on a governed run's diff, beside its declared checks (#187).

``forge.run_checks`` judges the change from the merge base with the base branch to
the head commit: ``secret_scan``, ``dependency_check`` and ``sast``
(``scf_policy``'s secret, dependency and SAST gates). Each is one check, failed on a
finding or when it could not verify, and recorded with the run as the declared
checks are. The secret scan reads every commit and commit message, as the pull
request carries them all. What the gates allow comes from the default branch's
policy, so a change cannot loosen its own gates. They read the diff and the files;
nothing from the change runs.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from scf_contracts.dtos import DiffGateRules
from scf_policy.dependency_gate import (
    DEPENDENCY_CHECK,
    Ecosystem,
    RegistryClient,
    check_dependencies,
    is_manifest,
    workspace_packages,
)
from scf_policy.diff_gate import GateFinding, GateResult, added_lines, gate_result
from scf_policy.sast_gate import SAST_CHECK, SAST_SUFFIXES, run_sast
from scf_policy.secret_gate import SECRET_SCAN_CHECK, scan_secrets
from scf_worker.run_workspace import CheckRun

__all__ = ["PublicRegistry", "diff_gate_checks"]

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


def _secret_scan(mirror: Path, fork: str, head: str, rules: DiffGateRules) -> GateResult:
    """Each commit's added lines and message: the pull request pushes every one."""
    findings: list[GateFinding] = []
    allowed: list[GateFinding] = []
    for commit in _git(mirror, "rev-list", "--reverse", f"{fork}..{head}").split():
        note = {"message": f"in commit {commit[:12]}"}
        message = _git(mirror, "log", "-1", "--format=%B", commit).split("\n")
        for result in (
            scan_secrets(
                added_lines(_git(mirror, "diff", *_PATCH, f"{commit}^", commit)),
                allowed_files=rules.secret_allowed_files,
            ),
            scan_secrets({"commit message": list(enumerate(message, start=1))}),  # no allowance
        ):
            findings += [f.model_copy(update=note) for f in result.findings]
            allowed += [f.model_copy(update=note) for f in result.allowed]
    return gate_result(findings, allowed=allowed)


def diff_gate_checks(
    mirror: Path, *, base: str, head: str, rules: DiffGateRules, registry: RegistryClient
) -> tuple[list[CheckRun], str | None]:
    """Run the three gates on ``base...head`` in ``mirror``, one check each.

    Also returns why to pause for a person: a gate the worker could not run (no
    analyser, no registry) is no fault of the builder's.
    """
    fork = _git(mirror, "merge-base", base, head).strip()
    listing = _git(mirror, "diff", "--name-status", "--no-renames", "-z", fork, head).split("\0")
    status = dict(zip(listing[1::2], listing[0::2], strict=False))  # path: A, M, T or D
    try:
        local = workspace_packages(_at(mirror, head, "pyproject.toml") or "")
    except ValueError:
        local = set()  # a root that does not parse names no workspace package
    gates: dict[str, Callable[[], GateResult]] = {
        SECRET_SCAN_CHECK: lambda: _secret_scan(mirror, fork, head, rules),
        DEPENDENCY_CHECK: lambda: check_dependencies(
            list(status),
            {
                p: (
                    "" if s == "A" else _at(mirror, fork, p),
                    "" if s == "D" else _at(mirror, head, p),
                )
                for p, s in status.items()
                if is_manifest(p)
            },
            registry,
            min_age_days=rules.min_dependency_age_days,
            allowed_packages=rules.allowed_packages,
            local_packages=local,
            now=datetime.now(UTC),
        ),
        SAST_CHECK: lambda: run_sast(
            {
                p: _at(mirror, head, p)
                for p, s in status.items()
                if s != "D" and p.endswith(SAST_SUFFIXES)
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
                output_tail=result.report(),
            )
        )
        if result.unavailable and pause is None:
            pause = f"{name} could not verify: {'; '.join(result.unavailable)}"
    return checks, pause


class PublicRegistry:
    """PyPI's and npm's public registries: the worker's :class:`RegistryClient`.

    A version's time is its first upload; with no version, the newest upload of a
    final release (no pre-release or yanked file, which a range does not admit). A
    PyPI pin matches its release ignoring trailing ``.0`` (``2.31`` is ``2.31.0``;
    shortcut: not full PEP 440 normalisation, which needs ``packaging``). A 404 is an
    unknown package; any other failure raises.
    """

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, timeout: float = 10.0
    ) -> None:
        self._http = httpx.Client(transport=transport, timeout=timeout)

    def _get(self, url: str) -> Any:
        response = self._http.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def published(self, ecosystem: Ecosystem, name: str, version: str | None) -> datetime | None:
        if ecosystem == "pypi":
            data = self._get(f"https://pypi.org/pypi/{quote(name, safe='')}/json")
            releases: dict[str, list[dict[str, Any]]] = (data or {}).get("releases", {})
            if version:
                files = [
                    f for v, fs in releases.items() if _trimmed(v) == _trimmed(version) for f in fs
                ]
            else:
                files = [
                    f
                    for v, fs in releases.items()
                    if _PEP440_FINAL.fullmatch(v)
                    for f in fs
                    if not f.get("yanked")
                ]
            uploads = [datetime.fromisoformat(f["upload_time_iso_8601"]) for f in files]
            return (min(uploads) if version else max(uploads)) if uploads else None
        data = self._get(f"https://registry.npmjs.org/{quote(name, safe='@')}") or {}
        versions = data.get("versions", {})
        times = {
            v: datetime.fromisoformat(t) for v, t in data.get("time", {}).items() if v in versions
        }
        if version:
            return times.get(version)
        return max((t for v, t in times.items() if _SEMVER_FINAL.fullmatch(v)), default=None)


_PEP440_FINAL = re.compile(r"\d+(?:\.\d+)*(?:\.post\d+)?")
_SEMVER_FINAL = re.compile(r"\d+\.\d+\.\d+")


def _trimmed(version: str) -> str:
    return re.sub(r"(?:\.0+)+$", "", version.lower())
