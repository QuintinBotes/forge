"""The SAST gate on an agent's diff (#187, spec 003 FR-019).

It runs the configured analyser over the changed files it reads (``bandit``: Python
files), written to a temporary directory, and fails on a finding at or above the
configured severity. ``# nosec`` comments are ignored: the change under review
cannot silence its own analyser. An analyser that is not installed, does not
finish, or cannot read a file gives ``could_not_verify``. Nothing from the change
is executed: bandit parses the source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from scf_policy.diff_gate import GateFinding, GateResult, gate_result

__all__ = ["SAST_CHECK", "SAST_SUFFIXES", "run_sast"]

#: The named check the SAST gate contributes to a run's check set.
SAST_CHECK = "sast"
#: The files bandit reads.
SAST_SUFFIXES = (".py",)
SAST_TIMEOUT_SECONDS = 300

_RANK = {"low": 0, "medium": 1, "high": 2}


def run_sast(
    files: dict[str, str], *, analyzer: str = "bandit", severity: str = "high"
) -> GateResult:
    """Analyse ``files`` (``{path: text}`` of the changed files) with ``analyzer``."""
    targets = {path: text for path, text in files.items() if path.endswith(SAST_SUFFIXES)}
    if not targets:
        return gate_result([])
    binary = shutil.which(analyzer)
    if binary is None:
        return gate_result([], reasons=[f"{analyzer} is not installed on this worker"])
    with tempfile.TemporaryDirectory(prefix="scf-sast-") as tmp:
        root = Path(tmp).resolve()
        written: dict[str, str] = {}
        for path, text in targets.items():
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                return gate_result([], reasons=[f"{path}: not a path inside the repository"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            written[str(target)] = path
        argv = [binary, "--format", "json", "--quiet", "--ignore-nosec", *written]
        try:
            done = subprocess.run(
                argv, cwd=root, capture_output=True, text=True, timeout=SAST_TIMEOUT_SECONDS
            )
            report = json.loads(done.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            return gate_result([], reasons=[f"{analyzer} did not finish ({type(exc).__name__})"])
    if done.returncode not in (0, 1):
        return gate_result([], reasons=[f"{analyzer} exited {done.returncode}"])
    reasons = [
        f"{written.get(e.get('filename'), e.get('filename'))}: {analyzer} could not read it"
        for e in report.get("errors", [])
    ]
    floor = _RANK[severity]
    findings = [
        GateFinding(
            file=written.get(r["filename"], r["filename"]),
            line=r["line_number"],
            rule=r["test_id"],
            # the rule's name, not its text: bandit's text can quote a hard-coded password
            message=f"{r['issue_severity'].lower()}: {r['test_name']}",
        )
        for r in report.get("results", [])
        if _RANK.get(str(r.get("issue_severity")).lower(), 2) >= floor
    ]
    return gate_result(findings, reasons=reasons)
