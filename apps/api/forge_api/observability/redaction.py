"""Secret redaction for logs, traces, audit entries, and span attributes.

Spec Security: "Secret redaction — Secrets stripped from logs, traces, and
retrieval results." This module is the shared primitive used by the audit
writer, the run-trace assembler, and the OpenTelemetry span hooks.

Redaction is intentionally conservative and value-preserving: only secret-named
keys and secret-shaped substrings are replaced with :data:`REDACTED`; everything
else is returned untouched so traces stay useful.
"""

from __future__ import annotations

import re
from typing import Any

#: The replacement token substituted for any redacted secret.
REDACTED = "[REDACTED]"

# Mapping keys whose *value* is always a secret regardless of its shape. Matched
# case-insensitively as a substring of the key (so ``db_password`` and
# ``aws_secret_access_key`` are caught too).
_SECRET_KEY_HINTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_token",
    "access_key",
    "private_key",
    "client_secret",
    "credential",
    "session_key",
)

# Secret-shaped substrings inside free text. Order matters only for readability;
# each pattern is applied independently.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization headers: "Bearer <token>" / "Basic <token>".
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]+"),
    # JSON Web Tokens (three base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # Provider-style API keys: sk-..., pk-..., rk-...
    re.compile(r"\b[a-z]{2}-[A-Za-z0-9]{16,}"),
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_, github_pat_.
    re.compile(r"\bgh[poursa]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    # AWS access key id.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def _key_is_secret(key: str) -> bool:
    """True when a mapping key names a secret value by convention."""
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def redact_text(text: str) -> str:
    """Return ``text`` with any secret-shaped substrings replaced by :data:`REDACTED`."""
    out = text
    for pattern in _SECRET_VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact a value of any JSON-ish shape.

    Strings are scanned for secret substrings; mappings and sequences are
    redacted element-wise; other scalars are returned unchanged.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``mapping`` with secret keys and values redacted.

    The input is never mutated. A key recognised as secret (e.g. ``password``,
    ``api_key``) has its value fully replaced; all other values are recursively
    scrubbed for secret-shaped substrings.
    """
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(key, str) and _key_is_secret(key):
            out[key] = REDACTED
        else:
            out[key] = redact_value(value)
    return out
