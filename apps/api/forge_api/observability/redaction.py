"""Secret redaction for logs, traces, audit entries, and span attributes.

Spec Security: "Secret redaction — Secrets stripped from logs, traces, and
retrieval results." This module is the shared primitive used by the audit
writer, the run-trace assembler, and the OpenTelemetry span hooks.

Redaction is intentionally conservative and value-preserving: only secret-named
keys and secret-shaped substrings are replaced with :data:`REDACTED`; everything
else is returned untouched so traces stay useful.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
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


# --------------------------------------------------------------------------- #
# Structural log redaction (HARD-13): a logging Filter installed on the root +
# service loggers scrubs every record's message and args *before* emission, so an
# accidental ``logger.info(secret)`` anywhere in the codebase cannot leak — the
# guarantee is sink-level, not call-site discipline.
# --------------------------------------------------------------------------- #

#: Loggers the filter is attached to by default (root + the ASGI/worker stacks).
_DEFAULT_REDACTED_LOGGERS: tuple[str, ...] = (
    "",  # root
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "gunicorn",
    "gunicorn.error",
    "celery",
)


class RedactingLogFilter(logging.Filter):
    """A ``logging.Filter`` that scrubs secret-shaped content from each record.

    Mutates ``record.msg`` and ``record.args`` in place (so the formatted message
    is redacted regardless of handler/formatter) and always returns ``True`` — it
    never drops a record. Idempotent and allocation-light: unchanged strings are
    left as-is and args are only rebuilt when they contain a secret shape.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            redacted = redact_text(record.msg)
            if redacted != record.msg:
                record.msg = redacted
        args = record.args
        if args:
            if isinstance(args, tuple):
                record.args = tuple(redact_value(a) for a in args)
            elif isinstance(args, dict):
                record.args = redact_mapping(args)
        return True


def install_log_redaction(extra_loggers: Sequence[str] = ()) -> RedactingLogFilter:
    """Attach a :class:`RedactingLogFilter` to the root + service loggers.

    Idempotent: a logger that already carries a :class:`RedactingLogFilter` is
    left untouched, so repeated app/worker startups do not stack filters. The
    filter is also attached to each logger's current handlers so records that
    only reach handler-level filtering are covered too. Returns the shared filter
    instance (handy for tests / manual attachment).
    """
    filt = RedactingLogFilter()
    names = list(dict.fromkeys((*_DEFAULT_REDACTED_LOGGERS, *extra_loggers)))
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingLogFilter) for f in logger.filters):
            logger.addFilter(filt)
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingLogFilter) for f in handler.filters):
                handler.addFilter(filt)
    return filt
