"""Audit boundary — every create / decision / resolution is written through an
injected sink (the foundation's immutable, hash-chained audit log; F39).

The protocol is shaped to the foundation's ``AuditLog.record_approval`` so the
composition root can pass that method's owner directly. ``workspace_id`` scopes
the row to its tenant; ``metadata`` carries a decision's surface and key id.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ApprovalAuditSink(Protocol):
    """The slice of the foundation ``AuditLog`` the approval service writes."""

    def record_approval(
        self,
        gate: str,
        *,
        status: str = "pending",
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        request_id: uuid.UUID | None = None,
        detail: str | None = None,
        workspace_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


class NullAuditSink:
    """No-op sink (explicit opt-out; tests use :class:`RecordingAuditSink`)."""

    def record_approval(
        self,
        gate: str,
        *,
        status: str = "pending",
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        request_id: uuid.UUID | None = None,
        detail: str | None = None,
        workspace_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class RecordingAuditSink:
    """Captures audit writes for assertions."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record_approval(
        self,
        gate: str,
        *,
        status: str = "pending",
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        request_id: uuid.UUID | None = None,
        detail: str | None = None,
        workspace_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "gate": gate,
            "status": status,
            "actor": actor,
            "run_id": run_id,
            "request_id": request_id,
            "detail": detail,
            "workspace_id": workspace_id,
            "metadata": metadata,
        }
        self.records.append(record)
        return record


__all__ = ["ApprovalAuditSink", "NullAuditSink", "RecordingAuditSink"]
