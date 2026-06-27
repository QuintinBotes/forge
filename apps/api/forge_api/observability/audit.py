"""Immutable, queryable audit log (Task 1.14 — observability + audit).

Spec Security: "Audit log — Every agent action, tool call, MCP call, and
approval — immutable, queryable." and "Secrets stripped from logs".

Design:
- :class:`AuditEntry` is a *frozen* Pydantic model — entries cannot be mutated
  after they are written.
- :class:`InMemoryAuditStore` is *append-only*: it exposes no update/delete API
  and stamps each entry with a monotonic ``seq`` plus a SHA-256 hash chain
  (``prev_hash`` -> ``entry_hash``) so any post-hoc tampering is detectable via
  :func:`verify_chain`.
- :class:`AuditLog` is the writer facade: it redacts secrets, hashes payloads,
  and records the four spec audit categories (agent action, tool call, MCP call,
  approval) plus system events.

The default store is in-memory so Phase-1 unit tests stay hermetic; a
Postgres-backed store can be swapped in at the :class:`AuditStore` protocol
boundary during Phase-2 integration.
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from forge_api.observability.redaction import redact_mapping, redact_text
from forge_contracts import MCPAuditEntry

#: Genesis hash that seeds the per-store audit chain.
GENESIS_HASH = "0" * 64


class AuditCategory(enum.StrEnum):
    """The kinds of action recorded in the audit log (spec: audit log scope)."""

    AGENT_ACTION = "agent_action"
    TOOL_CALL = "tool_call"
    MCP_CALL = "mcp_call"
    APPROVAL = "approval"
    POLICY_DECISION = "policy_decision"
    SYSTEM = "system"


def compute_payload_hash(payload: Any) -> str:
    """Return a stable SHA-256 hex digest of an arbitrary JSON-ish payload."""
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditEntry(BaseModel):
    """A single immutable audit record.

    ``seq``, ``prev_hash`` and ``entry_hash`` are stamped by the store on append;
    callers leave them at their defaults.
    """

    model_config = ConfigDict(frozen=True)

    seq: int = -1
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    category: AuditCategory
    action: str
    actor: str | None = None
    workspace_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    target: str | None = None
    connection_id: str | None = None
    status: str = "ok"
    detail: str | None = None
    payload_hash: str | None = None
    latency_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    redacted: bool = True
    prev_hash: str | None = None
    entry_hash: str | None = None


def _hash_entry(entry: AuditEntry) -> str:
    """Compute the content hash of an entry (excluding its own ``entry_hash``)."""
    content = entry.model_dump(mode="json", exclude={"entry_hash"})
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(entries: list[AuditEntry]) -> bool:
    """Return ``True`` iff ``entries`` form an untampered, in-order hash chain."""
    prev = GENESIS_HASH
    for position, entry in enumerate(entries):
        if entry.seq != position:
            return False
        if entry.prev_hash != prev:
            return False
        recomputed = _hash_entry(entry.model_copy(update={"entry_hash": None}))
        if recomputed != entry.entry_hash:
            return False
        prev = entry.entry_hash or ""
    return True


@runtime_checkable
class AuditStore(Protocol):
    """Append-only storage boundary for audit entries (Phase-2 may back with DB)."""

    def append(self, entry: AuditEntry) -> AuditEntry: ...

    def all(self) -> list[AuditEntry]: ...

    def query(
        self,
        *,
        category: AuditCategory | None = None,
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        connection_id: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]: ...

    def verify_integrity(self) -> bool: ...


class InMemoryAuditStore:
    """Append-only in-memory audit store with a tamper-evident hash chain.

    Intentionally exposes *no* mutation/delete surface: immutability is enforced
    structurally, not by convention.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def append(self, entry: AuditEntry) -> AuditEntry:
        seq = len(self._entries)
        prev = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
        stamped = entry.model_copy(update={"seq": seq, "prev_hash": prev, "entry_hash": None})
        final = stamped.model_copy(update={"entry_hash": _hash_entry(stamped)})
        self._entries.append(final)
        return final

    def all(self) -> list[AuditEntry]:
        return list(self._entries)

    def query(
        self,
        *,
        category: AuditCategory | None = None,
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        connection_id: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]:
        rows = self._entries
        if category is not None:
            rows = [e for e in rows if e.category is category]
        if actor is not None:
            rows = [e for e in rows if e.actor == actor]
        if run_id is not None:
            rows = [e for e in rows if e.run_id == run_id]
        if connection_id is not None:
            rows = [e for e in rows if e.connection_id == connection_id]
        if limit is not None and limit >= 0:
            rows = rows[-limit:] if limit else []
        return list(rows)

    def verify_integrity(self) -> bool:
        return verify_chain(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


class AuditLog:
    """Writer facade over an :class:`AuditStore` with redaction + hashing."""

    def __init__(self, store: AuditStore | None = None) -> None:
        self._store: AuditStore = store or InMemoryAuditStore()

    @property
    def store(self) -> AuditStore:
        return self._store

    def record(
        self,
        *,
        category: AuditCategory,
        action: str,
        actor: str | None = None,
        workspace_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        target: str | None = None,
        connection_id: str | None = None,
        status: str = "ok",
        detail: str | None = None,
        payload: Any | None = None,
        payload_hash: str | None = None,
        latency_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Redact, hash, and append an audit entry; returns the stamped record."""
        resolved_hash = payload_hash
        if resolved_hash is None and payload is not None:
            resolved_hash = compute_payload_hash(payload)
        entry = AuditEntry(
            category=category,
            action=action,
            actor=actor,
            workspace_id=workspace_id,
            run_id=run_id,
            target=target,
            connection_id=connection_id,
            status=status,
            detail=redact_text(detail) if detail else None,
            payload_hash=resolved_hash,
            latency_ms=latency_ms,
            metadata=redact_mapping(metadata or {}),
            redacted=True,
        )
        return self._store.append(entry)

    def record_agent_action(
        self,
        action: str,
        *,
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        detail: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        return self.record(
            category=AuditCategory.AGENT_ACTION,
            action=action,
            actor=actor,
            run_id=run_id,
            detail=detail,
            metadata=metadata,
        )

    def record_tool_call(
        self,
        tool: str,
        *,
        action: str = "call",
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        arguments: dict[str, Any] | None = None,
        status: str = "ok",
        latency_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        return self.record(
            category=AuditCategory.TOOL_CALL,
            action=action,
            actor=actor,
            run_id=run_id,
            target=tool,
            status=status,
            payload=arguments,
            latency_ms=latency_ms,
            metadata=metadata,
        )

    def record_mcp_call(
        self,
        entry: MCPAuditEntry,
        *,
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> AuditEntry:
        """Record an MCP call from the frozen contract :class:`MCPAuditEntry`."""
        return self.record(
            category=AuditCategory.MCP_CALL,
            action=entry.tool,
            actor=actor,
            run_id=run_id,
            target=entry.tool,
            connection_id=entry.connection_id,
            status=entry.status,
            payload_hash=entry.payload_hash,
            latency_ms=entry.latency_ms,
        )

    def record_approval(
        self,
        gate: str,
        *,
        status: str = "pending",
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        request_id: uuid.UUID | None = None,
        detail: str | None = None,
    ) -> AuditEntry:
        return self.record(
            category=AuditCategory.APPROVAL,
            action=gate,
            actor=actor,
            run_id=run_id,
            status=status,
            detail=detail,
            metadata={"request_id": str(request_id)} if request_id else None,
        )

    def query(
        self,
        *,
        category: AuditCategory | None = None,
        actor: str | None = None,
        run_id: uuid.UUID | None = None,
        connection_id: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]:
        return self._store.query(
            category=category,
            actor=actor,
            run_id=run_id,
            connection_id=connection_id,
            limit=limit,
        )

    def verify_integrity(self) -> bool:
        return self._store.verify_integrity()
