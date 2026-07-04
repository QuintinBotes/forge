"""Thin adapter over F37's canonical ``forge_auth.redaction.SecretRedactor``.

F38 adds **no** new patterns (spec §4 "consumed contracts"): one shared redactor
instance is used as the LAST processor before any log/span/metric egress so
secrets never reach a sink. ``register_known_secret`` lets the BYOK vault pin
actual stored secret values for exact-match scrubbing.
"""

from __future__ import annotations

from typing import Any

from forge_auth.redaction import REDACTED, SecretRedactor

_redactor = SecretRedactor()


def get_redactor() -> SecretRedactor:
    """The process-wide shared redactor (one registry of known secrets)."""
    return _redactor


def register_known_secret(value: str) -> None:
    """Pin an actual secret value (e.g. a BYOK key) for exact-match scrubbing."""
    _redactor.register_known_secret(value)


def redact_text(text: str) -> str:
    """Redact secret-shaped content from free text."""
    return _redactor.redact(text)


def redact_value(value: Any) -> Any:
    """Recursively redact a JSON-ish structure (dict/list/tuple/str)."""
    return _redactor.redact_value(value)


__all__ = ["REDACTED", "get_redactor", "redact_text", "redact_value", "register_known_secret"]
