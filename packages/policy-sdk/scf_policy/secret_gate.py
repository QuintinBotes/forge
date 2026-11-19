"""The secret gate on an agent's diff (#187, spec 003 FR-016).

It reads only the lines the diff adds, against a small set of high-precision
credential shapes named as gitleaks names its rules: a match blocks the run, so a
shape must not match ordinary code. A finding names the file, line and rule, never
the value. The repository policy may allow matches in a named file
(``diff_gates.secret_allowed_files``); they are recorded as allowed.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from scf_policy.diff_gate import GateFinding, GateResult, gate_result

__all__ = ["SECRET_RULES", "SECRET_SCAN_CHECK", "scan_secrets"]

#: The named check the secret gate contributes to a run's check set.
SECRET_SCAN_CHECK = "secret_scan"

SECRET_RULES: dict[str, re.Pattern[str]] = {
    "private-key": re.compile(r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"),
    "aws-access-token": re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{82})\b"),
    "gitlab-pat": re.compile(r"\bglpat-[0-9A-Za-z_-]{20}\b"),
    "slack-token": re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}"),
    "stripe-secret-key": re.compile(r"\b[rs]k_live_[0-9A-Za-z]{24,}"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "anthropic-api-key": re.compile(r"\bsk-ant-(?:api|admin)\d\d-[0-9A-Za-z_-]{80,}"),
    "openai-api-key": re.compile(r"\bsk-[0-9A-Za-z_-]{20,}T3BlbkFJ[0-9A-Za-z_-]{20,}"),
    "npm-access-token": re.compile(r"\bnpm_[0-9A-Za-z]{36}\b"),
    "pypi-upload-token": re.compile(r"\bpypi-AgEIcHlwaS5vcmc[0-9A-Za-z_-]{50,}"),
}


def scan_secrets(
    added: dict[str, list[tuple[int, str]]], *, allowed_files: Collection[str] = ()
) -> GateResult:
    """Scan ``added`` (``{path: [(line, text)]}``, see ``added_lines``) for credentials.

    A match in a file named in ``allowed_files`` (exactly that path) is allowed.
    """
    findings: list[GateFinding] = []
    allowed: list[GateFinding] = []
    for path in sorted(added):
        for number, text in added[path]:
            for rule, pattern in SECRET_RULES.items():
                if pattern.search(text):
                    finding = GateFinding(file=path, line=number, rule=rule)
                    (allowed if path in allowed_files else findings).append(finding)
    return gate_result(findings, allowed=allowed)
