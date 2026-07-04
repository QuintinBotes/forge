"""Deep-walk redaction hook for audit metadata (F39 §"Secret redaction").

F39 defines **no pattern set of its own** — the canonical patterns + entropy +
known-secret registry live in F37's ``forge_auth.redaction.SecretRedactor``.
``forge_db`` must not depend on ``forge_auth`` (layering), so the redactor is
duck-typed here: anything exposing ``redact_value(Any) -> Any`` (the
``SecretRedactor`` shape) or a plain string ``redact(str) -> str`` works.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["MetadataRedactor", "redact_metadata"]


@runtime_checkable
class MetadataRedactor(Protocol):
    """The slice of F37's ``SecretRedactor`` the audit writer needs."""

    def redact(self, text: str) -> str: ...


def redact_metadata(value: Any, redactor: object | None) -> Any:
    """Recursively redact a JSON-ish value through ``redactor``.

    Prefers the redactor's own recursive ``redact_value`` (F37's
    ``SecretRedactor`` provides it); otherwise deep-walks dict/list/tuple
    structures applying string-level ``redact`` to every string leaf.
    ``redactor=None`` is the identity (producers that pre-redact, unit fakes).
    """
    if redactor is None:
        return value
    redact_value = getattr(redactor, "redact_value", None)
    if callable(redact_value):
        return redact_value(value)
    if isinstance(value, str):
        return redactor.redact(value)  # type: ignore[attr-defined]
    if isinstance(value, dict):
        return {k: redact_metadata(v, redactor) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_metadata(v, redactor) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_metadata(v, redactor) for v in value)
    return value
