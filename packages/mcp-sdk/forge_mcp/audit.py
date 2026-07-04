"""Immutable MCP audit log (plan Task 1.12; spec MCP rule 4).

Every tool invocation produces an :class:`~forge_contracts.MCPAuditEntry`
recording the tool name, a redacted payload hash, the result status, and
latency. The in-memory sink is append-only; the API layer can swap a
DB-backed :class:`AuditSink` without changing the client.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from forge_contracts import MCPAuditEntry
from forge_mcp.security import payload_hash


def build_audit_entry(
    *,
    connection_id: str,
    tool: str,
    arguments: Any,
    status: str,
    latency_ms: int | None = None,
) -> MCPAuditEntry:
    """Construct a redacted, timestamped audit entry for a tool call."""
    return MCPAuditEntry(
        connection_id=connection_id,
        tool=tool,
        payload_hash=payload_hash(arguments),
        status=status,
        latency_ms=latency_ms,
        timestamp=datetime.now(UTC),
        redacted=True,
    )


@runtime_checkable
class AuditSink(Protocol):
    """A destination for audit entries (in-memory, DB, OTel, ...)."""

    def record(self, entry: MCPAuditEntry) -> None: ...


class InMemoryAuditLog:
    """Append-only in-memory audit log.

    ``entries`` is read-only and returns a copy, so callers can neither replace
    the backing list nor mutate the recorded history.
    """

    def __init__(self) -> None:
        self._entries: list[MCPAuditEntry] = []

    def record(self, entry: MCPAuditEntry) -> None:
        self._entries.append(entry)

    @property
    def entries(self) -> list[MCPAuditEntry]:
        return list(self._entries)

    def for_connection(self, connection_id: str) -> list[MCPAuditEntry]:
        return [e for e in self._entries if e.connection_id == connection_id]

    def __len__(self) -> int:
        return len(self._entries)


class TeeAuditLog(InMemoryAuditLog):
    """An :class:`InMemoryAuditLog` that also fans each entry out to more sinks.

    Used by the live gateway/API wiring (HARD-05): the manager keeps an
    in-memory trail (so ``GET …/audit`` still reads back) *while* every entry is
    forwarded to a durable platform sink (Postgres ``AuditStore`` via the
    ``forge_api`` bridge). Subclassing :class:`InMemoryAuditLog` preserves the
    ``isinstance`` the manager uses to expose the trail. A downstream sink that
    raises never corrupts the in-memory record — the entry is appended first and
    forwarding failures are swallowed so auditing can never break a live call.
    """

    def __init__(self, *sinks: AuditSink) -> None:
        super().__init__()
        self._sinks: tuple[AuditSink, ...] = sinks

    def record(self, entry: MCPAuditEntry) -> None:
        super().record(entry)
        for sink in self._sinks:
            with contextlib.suppress(Exception):  # pragma: no cover - durability best-effort
                sink.record(entry)


__all__ = ["AuditSink", "InMemoryAuditLog", "TeeAuditLog", "build_audit_entry"]
