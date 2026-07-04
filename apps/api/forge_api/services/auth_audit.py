"""F37 auth-domain audit emission: ``Principal`` → canonical ``AuditEvent``.

A thin producer helper (NOT a sink implementation): maps the request
:class:`~forge_api.deps.Principal` onto the shared
:class:`forge_contracts.audit.AuditEvent` contract owned by
``cross-cutting/F39-audit-log`` and emits through an injected ``AuditSink``.
The concrete sink is F39's ``SqlAuditWriter`` when a DB session is in play, an
in-memory fake in tests, and the :class:`LoggingAuditSink` fallback otherwise —
so no auth/secret/key mutation event is ever silently dropped.

Every event's ``details`` payload is scrubbed through the canonical
``forge_auth`` :class:`~forge_auth.redaction.SecretRedactor` before emission
(AC16: no secret ever lands in audit).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from forge_api.deps import Principal
from forge_auth.redaction import SecretRedactor
from forge_contracts.audit import AuditEvent, AuditSink

__all__ = ["AuthAuditEmitter", "LoggingAuditSink", "actor_type_for"]

logger = logging.getLogger("forge.audit")


class LoggingAuditSink:
    """Log-only fallback ``AuditSink`` — used until a durable sink is injected."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self._log = log or logger

    def emit(self, event: AuditEvent) -> None:
        self._log.info(
            "audit action=%s workspace=%s actor=%s actor_type=%s target=%s:%s result=%s",
            event.action,
            event.workspace_id,
            event.actor_id,
            event.actor_type,
            event.target_type,
            event.target_id,
            event.result,
        )


def actor_type_for(principal: Principal) -> str:
    """Map an authenticated principal onto the audit ``actor_type`` vocabulary."""
    if principal.auth_method == "api_key":
        return "agent_runner" if principal.role.value == "agent-runner" else "api_key"
    if principal.auth_method == "service":
        return "system"
    return "user"


class AuthAuditEmitter:
    """Emit auth-domain events (login/key/secret mutations) through a sink."""

    def __init__(
        self,
        sink: AuditSink | None = None,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.sink: AuditSink = sink or LoggingAuditSink()
        self.redactor = redactor or SecretRedactor()

    def emit(
        self,
        *,
        action: str,
        principal: Principal,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
        result: str = "success",
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Build, redact, and emit one canonical auth-domain ``AuditEvent``."""
        event = AuditEvent(
            workspace_id=principal.workspace_id,
            action=action,
            actor_id=principal.user_id,
            actor_type=actor_type_for(principal),
            target_type=target_type,
            target_id=target_id,
            result=result,
            details=self.redactor.redact_value(details or {}),
        )
        self.sink.emit(event)
        return event
