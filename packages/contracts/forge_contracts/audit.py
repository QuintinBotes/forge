"""Immutable audit-log contract (the genuinely-missing F39 sliver F30 needs).

The idealized slice doc emits every authorization change through a *canonical*
``forge_contracts.audit.AuditSink`` owned by ``cross-cutting/F39-audit-log`` and
persisted into a shared append-only ``audit_log`` table. Neither the contract
nor the table exist in-tree, so F30 introduces the minimal, general pieces it
needs here (kept deliberately small — a full F39 query/retention surface is out
of scope): the :class:`AuditEvent` DTO and the :class:`AuditSink` Protocol. The
ORM ``AuditLog`` model + ``SqlAuditWriter`` live in ``forge_db`` /
``forge_api`` respectively.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AuditEvent", "AuditSink"]


class AuditEvent(BaseModel):
    """A single immutable audit record.

    ``action`` is a dotted vocabulary string (e.g. ``role_grant.created``,
    ``team.archived``); ``before``/``after`` capture the change for grant/role
    mutations; ``details`` carries any extra structured context.
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: UUID
    action: str
    actor_id: UUID | None = None
    actor_type: str = "user"
    target_type: str | None = None
    target_id: UUID | None = None
    scope_type: str | None = None
    scope_id: UUID | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    result: str = "success"
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


@runtime_checkable
class AuditSink(Protocol):
    """A durable, append-only destination for :class:`AuditEvent` records."""

    def emit(self, event: AuditEvent) -> None: ...
