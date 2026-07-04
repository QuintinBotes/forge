"""Unit tests for the MCP -> platform audit bridge (HARD-05 AC8, offline).

:class:`~forge_api.observability.MCPAuditSink` forwards each frozen
``MCPAuditEntry`` the SDK emits onto the durable, redacted, hash-chained platform
audit log as an ``MCPAuditEntry``-category row. This runs in the default gate
against the in-memory platform store (the durable-Postgres assertion is the
integration lane's job).
"""

from __future__ import annotations

import uuid

from forge_api.observability import AuditCategory, AuditLog, MCPAuditSink
from forge_contracts import MCPAuditEntry
from forge_mcp import TeeAuditLog
from forge_mcp.audit import build_audit_entry


def test_bridge_forwards_mcp_entry_to_platform_log() -> None:
    log = AuditLog()
    sink = MCPAuditSink(log)
    entry = build_audit_entry(
        connection_id="confluence-engineering",
        tool="resources/read",
        arguments={"uri": "confluence://engineering/page-1", "token": "should-not-leak"},
        status="ok",
        latency_ms=12,
    )
    sink.record(entry)

    rows = log.query(category=AuditCategory.MCP_CALL)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "resources/read"
    assert row.connection_id == "confluence-engineering"
    assert row.status == "ok"
    assert row.payload_hash == entry.payload_hash
    assert row.latency_ms == 12
    assert row.redacted is True
    # No secret ever crosses the bridge (only the secret-free payload hash does).
    assert "should-not-leak" not in row.model_dump_json()


def test_bridge_carries_actor_and_workspace() -> None:
    ws = uuid.uuid4()
    log = AuditLog()
    sink = MCPAuditSink(log, actor="agent-runner", workspace_id=ws)
    sink.record(
        build_audit_entry(connection_id="c", tool="tools/call", arguments={"x": 1}, status="ok")
    )
    row = log.query(category=AuditCategory.MCP_CALL)[0]
    assert row.actor == "agent-runner"
    assert row.workspace_id == ws


def test_tee_audit_log_keeps_inmemory_and_forwards() -> None:
    log = AuditLog()
    tee = TeeAuditLog(MCPAuditSink(log))
    tee.record(
        build_audit_entry(connection_id="c", tool="resources/list", arguments={}, status="ok")
    )
    # In-memory trail preserved for GET …/audit read-back.
    assert len(tee.entries) == 1
    # Durable platform row also written.
    assert len(log.query(category=AuditCategory.MCP_CALL)) == 1


def test_tee_audit_log_swallows_sink_failure() -> None:
    class _Boom:
        def record(self, entry: MCPAuditEntry) -> None:
            raise RuntimeError("downstream is down")

    tee = TeeAuditLog(_Boom())
    # A failing durable sink must never break the in-memory audit / the live call.
    tee.record(build_audit_entry(connection_id="c", tool="t", arguments={}, status="ok"))
    assert len(tee.entries) == 1


def test_platform_chain_stays_verifiable_after_bridge() -> None:
    log = AuditLog()
    sink = MCPAuditSink(log)
    for i in range(3):
        sink.record(
            build_audit_entry(connection_id="c", tool=f"t{i}", arguments={"i": i}, status="ok")
        )
    assert log.verify_integrity() is True
