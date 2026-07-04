"""Canonical ``SecretRedactor`` (F37): patterns + entropy + known-secret registry.

One redactor instance is meant to be shared per process (or per workspace for
the dynamic registry) and applied at every egress point: log lines, run
traces, audit-event metadata, and MCP gateway responses. It complements the
value-shape helpers in ``forge_api.observability.redaction`` (F38) with a
stateful registry of *actual* stored secret values plus an entropy heuristic.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = ["REDACTED", "SecretRedactor"]

#: Replacement token substituted for anything redacted.
REDACTED = "[REDACTED]"

#: Secret-shaped substrings (applied to free text).
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private-key blocks (multi-line).
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # KEY= / TOKEN= / SECRET= / PASSWORD= style assignments — keep the name,
    # scrub the value.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?)"
        r"(\s*[=:]\s*)([^\s'\",;]+)"
    ),
    # Authorization headers: "Bearer <token>" / "Basic <token>".
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"),
    # JSON Web Tokens (three base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # AWS access key id + provider-style keys (sk-..., forge_..., ghp_...).
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[a-z]{2}-(?:[a-z]+-)?[A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[poursa]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bforge_(?:pat|svc|agt|int)_[A-Za-z0-9_\-]{8,}"),
)

#: Candidate tokens for the entropy heuristic: long, unbroken, secret-alphabet.
_ENTROPY_CANDIDATE = re.compile(r"\b[A-Za-z0-9+/=_\-]{28,}\b")
_ENTROPY_THRESHOLD_BITS = 3.8
#: UUIDs are ubiquitous in metadata and not secrets — never entropy-redacted.
_UUID_SHAPE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _shannon_entropy(text: str) -> float:
    """Shannon entropy in bits/char (high for random key material)."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _looks_high_entropy(candidate: str) -> bool:
    if _UUID_SHAPE.match(candidate):
        return False
    # Require a mixed alphabet so long ordinary words / repeated ids survive.
    has_digit = any(c.isdigit() for c in candidate)
    has_alpha = any(c.isalpha() for c in candidate)
    if not (has_digit and has_alpha):
        return False
    return _shannon_entropy(candidate) >= _ENTROPY_THRESHOLD_BITS


class SecretRedactor:
    """Regex + entropy + dynamic known-secret scrubber. Satisfies the
    ``forge_contracts.auth.SecretRedactor`` protocol."""

    def __init__(self) -> None:
        self._known: set[str] = set()

    def register_known_secret(self, value: str) -> None:
        """Register a stored secret value so it is scrubbed wherever it appears.

        Trivially short values are ignored (redacting them would mangle
        ordinary text more than it would protect anything).
        """
        if value and len(value) >= 6:
            self._known.add(value)

    def redact(self, text: str) -> str:
        """Return ``text`` with known secrets and secret-shaped content removed."""
        out = text
        # Known values first — exact matches beat any pattern.
        for value in sorted(self._known, key=len, reverse=True):
            out = out.replace(value, REDACTED)
        for pattern in _PATTERNS:
            if pattern.groups >= 3:  # assignment pattern: keep name + separator
                out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
            else:
                out = pattern.sub(REDACTED, out)
        out = _ENTROPY_CANDIDATE.sub(
            lambda m: REDACTED if _looks_high_entropy(m.group(0)) else m.group(0), out
        )
        return out

    def redact_value(self, value: Any) -> Any:
        """Recursively redact a JSON-ish value (for audit-event metadata)."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(v) for v in value)
        return value
