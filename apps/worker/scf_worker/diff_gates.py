"""The gates on a governed run's diff, beside its declared checks (#187).

``forge.run_checks`` judges the change from the merge base with the base branch to
the head commit: ``secret_scan``, ``dependency_check`` and ``sast``
(``scf_policy``'s secret, dependency and SAST gates). Each is one check, failed on a
finding or when it could not verify, and recorded with the run as the declared
checks are. What the gates allow comes from the default branch's policy, so a
change cannot loosen its own gates. They read the diff and the files; nothing from
the change runs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import chain
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
)
from scf_policy.diff_gate import GateResult, added_lines
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


def _at(repo: Path, ref: str, path: str) -> str:
    """``path`` at ``ref``, or nothing when it is not there."""
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return ""


def diff_gate_checks(
    mirror: Path, *, base: str, head: str, rules: DiffGateRules, registry: RegistryClient
) -> list[CheckRun]:
    """Run the three gates on ``base...head`` in ``mirror``, one check each."""
    fork = _git(mirror, "merge-base", base, head).strip()
    # --text: a .gitattributes in the change cannot hide a file's lines as binary
    diff = _git(
        mirror, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "-U0", fork, head
    )
    changed = [p for p in _git(mirror, "diff", "--name-only", "-z", fork, head).split("\0") if p]
    added = added_lines(diff)
    gates: dict[str, Callable[[], GateResult]] = {
        SECRET_SCAN_CHECK: lambda: scan_secrets(added, allowed_files=rules.secret_allowed_files),
        DEPENDENCY_CHECK: lambda: check_dependencies(
            changed,
            {p: (_at(mirror, fork, p), _at(mirror, head, p)) for p in changed if is_manifest(p)},
            registry,
            min_age_days=rules.min_dependency_age_days,
            allowed_packages=rules.allowed_packages,
            now=datetime.now(UTC),
        ),
        SAST_CHECK: lambda: run_sast(
            {p: _at(mirror, head, p) for p in added if p.endswith(SAST_SUFFIXES)},
            analyzer=rules.sast_analyzer,
            severity=rules.sast_severity,
        ),
    }
    checks = []
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
    return checks


class PublicRegistry:
    """PyPI's and npm's public registries: the worker's :class:`RegistryClient`.

    A version's time is its first upload; with no version, the newest upload of any
    release. A 404 is an unknown package; any other failure raises.
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
            releases = (data or {}).get("releases", {})
            files = (
                releases.get(version, [])
                if version
                else list(chain.from_iterable(releases.values()))
            )
            uploads = [datetime.fromisoformat(f["upload_time_iso_8601"]) for f in files]
            return (min(uploads) if version else max(uploads)) if uploads else None
        data = self._get(f"https://registry.npmjs.org/{quote(name, safe='@')}") or {}
        versions = data.get("versions", {})
        times = {
            v: datetime.fromisoformat(t) for v, t in data.get("time", {}).items() if v in versions
        }
        return times.get(version) if version else max(times.values(), default=None)
