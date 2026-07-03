"""Secret-redaction boundary.

F36 does NOT define its own redaction patterns: the canonical filter is the
foundation's ``forge_api.observability.redaction`` (the F37 secret redactor in
this codebase). This package only defines the injection point — the composition
root passes the real ``redact_mapping``; hermetic tests pass a recording fake.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: A redactor takes a JSON-ish mapping and returns a redacted deep copy.
Redactor = Callable[[dict[str, Any]], dict[str, Any]]


def passthrough_redactor(payload: dict[str, Any]) -> dict[str, Any]:
    """Identity redactor for hermetic unit tests (NOT for production wiring)."""
    return dict(payload)


__all__ = ["Redactor", "passthrough_redactor"]
