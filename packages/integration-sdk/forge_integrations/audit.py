"""Framework-agnostic audit events for the GitHub client (HARD-01 §3.2).

The SDK emits a :class:`GitHubAuditEvent` per terminal request outcome through an
injected :data:`AuditSink`. This module deliberately imports **nothing** from
``forge_api`` so the layering (SDK below the API) is preserved: the API wiring
layer adapts these events onto the immutable, hash-chained
``forge_api.observability.audit.AuditLog`` and applies redaction there.

Every field is safe to persist: ``payload_hash`` is the SHA-256 of a request
body (never the body itself) and ``detail`` carries only provider-message text,
never a token, JWT, or PEM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class GitHubAuditEvent:
    """One GitHub operation's audit record (no secrets — see module docstring)."""

    action: str
    repo: str | None = None
    status: str = "ok"
    status_code: int | None = None
    latency_ms: int | None = None
    payload_hash: str | None = None
    detail: str | None = None


#: A consumer of audit events. Implementations must never raise back into the
#: client's request path (the client guards the call, but sinks should be cheap).
AuditSink = Callable[[GitHubAuditEvent], None]


__all__ = ["AuditSink", "GitHubAuditEvent"]
