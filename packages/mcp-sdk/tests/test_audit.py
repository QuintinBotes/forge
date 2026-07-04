"""Tests for the MCP audit log (plan Task 1.12; spec MCP rule 4).

Every tool call records: tool name, payload hash, result status, latency. The
log is append-only and secrets are redacted before anything is persisted.
"""

from __future__ import annotations

import pytest

from forge_contracts import MCPAuditEntry
from forge_mcp.audit import InMemoryAuditLog, build_audit_entry


def test_build_audit_entry_records_required_fields() -> None:
    entry = build_audit_entry(
        connection_id="confluence",
        tool="search_pages",
        arguments={"q": "vault", "token": "secret"},
        status="ok",
        latency_ms=12,
    )
    assert isinstance(entry, MCPAuditEntry)
    assert entry.connection_id == "confluence"
    assert entry.tool == "search_pages"
    assert entry.status == "ok"
    assert entry.latency_ms == 12
    assert entry.payload_hash and len(entry.payload_hash) == 64
    assert entry.redacted is True
    assert entry.timestamp is not None


def test_audit_payload_hash_excludes_secret_value() -> None:
    with_secret = build_audit_entry(
        connection_id="c", tool="t", arguments={"q": "x", "token": "AAA"}, status="ok"
    )
    other_secret = build_audit_entry(
        connection_id="c", tool="t", arguments={"q": "x", "token": "BBB"}, status="ok"
    )
    # Redacted-before-hash: differing secrets produce the same hash.
    assert with_secret.payload_hash == other_secret.payload_hash
    # The raw secret never appears in the serialized entry.
    assert "AAA" not in with_secret.model_dump_json()


def test_in_memory_audit_log_is_append_only() -> None:
    log = InMemoryAuditLog()
    e1 = build_audit_entry(connection_id="c", tool="a", arguments={}, status="ok")
    e2 = build_audit_entry(connection_id="c", tool="b", arguments={}, status="error")
    log.record(e1)
    log.record(e2)
    assert [e.tool for e in log.entries] == ["a", "b"]
    # The returned view is a copy: mutating it does not corrupt the log.
    snapshot = log.entries
    snapshot.clear()
    assert len(log.entries) == 2


def test_audit_log_filters_by_connection() -> None:
    log = InMemoryAuditLog()
    log.record(build_audit_entry(connection_id="c1", tool="a", arguments={}, status="ok"))
    log.record(build_audit_entry(connection_id="c2", tool="b", arguments={}, status="ok"))
    only_c1 = log.for_connection("c1")
    assert [e.tool for e in only_c1] == ["a"]


def test_audit_log_cannot_be_overwritten_in_place() -> None:
    log = InMemoryAuditLog()
    log.record(build_audit_entry(connection_id="c", tool="a", arguments={}, status="ok"))
    with pytest.raises(AttributeError):
        log.entries = []  # type: ignore[misc]
