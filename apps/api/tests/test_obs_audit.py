"""Tests for the immutable audit log writer (Task 1.14 — observability + audit).

Spec Security: "Audit log — Every agent action, tool call, MCP call, and
approval — immutable, queryable" and "Secrets stripped from logs".
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from forge_api.observability.audit import (
    AuditCategory,
    AuditEntry,
    AuditLog,
    InMemoryAuditStore,
    compute_payload_hash,
    verify_chain,
)
from forge_contracts import MCPAuditEntry


def test_record_assigns_monotonic_sequence_numbers() -> None:
    log = AuditLog()
    a = log.record(category=AuditCategory.AGENT_ACTION, action="plan")
    b = log.record(category=AuditCategory.TOOL_CALL, action="write_file")
    c = log.record(category=AuditCategory.APPROVAL, action="approve")
    assert [a.seq, b.seq, c.seq] == [0, 1, 2]


def test_audit_entry_is_immutable() -> None:
    log = AuditLog()
    entry = log.record(category=AuditCategory.TOOL_CALL, action="write_file")
    with pytest.raises(ValidationError):
        entry.action = "tampered"


def test_store_is_append_only_no_mutation_api() -> None:
    store = InMemoryAuditStore()
    # An append-only store exposes no delete/update/clear surface.
    assert not hasattr(store, "delete")
    assert not hasattr(store, "update")
    assert not hasattr(store, "remove")


def test_record_redacts_secrets_in_metadata_and_detail() -> None:
    log = AuditLog()
    entry = log.record(
        category=AuditCategory.TOOL_CALL,
        action="call_api",
        detail="used Authorization: Bearer abcDEF123456ghiJKL",
        metadata={"api_key": "sk-SECRET1234567890", "endpoint": "/v1/things"},
    )
    assert entry.redacted is True
    assert "abcDEF123456ghiJKL" not in (entry.detail or "")
    assert entry.metadata["api_key"] != "sk-SECRET1234567890"
    assert entry.metadata["endpoint"] == "/v1/things"


def test_record_tool_call_hashes_payload_without_storing_raw_secret() -> None:
    log = AuditLog()
    args = {"path": "app/main.py", "token": "sk-DONOTLEAK1234567890"}
    entry = log.record_tool_call("write_file", arguments=args)
    assert entry.category is AuditCategory.TOOL_CALL
    assert entry.target == "write_file"
    assert entry.payload_hash == compute_payload_hash(args)
    # The raw secret never appears anywhere in the serialized entry.
    assert "sk-DONOTLEAK1234567890" not in entry.model_dump_json()


def test_record_mcp_call_maps_from_contract_dto() -> None:
    log = AuditLog()
    mcp = MCPAuditEntry(
        connection_id="conn-1",
        tool="search_docs",
        payload_hash="abc123",
        status="ok",
        latency_ms=42,
    )
    entry = log.record_mcp_call(mcp)
    assert entry.category is AuditCategory.MCP_CALL
    assert entry.connection_id == "conn-1"
    assert entry.target == "search_docs"
    assert entry.status == "ok"
    assert entry.latency_ms == 42
    assert entry.payload_hash == "abc123"


def test_query_filters_by_category_and_run_id() -> None:
    log = AuditLog()
    run = uuid.uuid4()
    log.record(category=AuditCategory.AGENT_ACTION, action="plan", run_id=run)
    log.record(category=AuditCategory.TOOL_CALL, action="write", run_id=run)
    log.record(category=AuditCategory.TOOL_CALL, action="write", run_id=uuid.uuid4())

    by_run = log.query(run_id=run)
    assert len(by_run) == 2
    by_cat = log.query(category=AuditCategory.TOOL_CALL)
    assert len(by_cat) == 2
    both = log.query(category=AuditCategory.TOOL_CALL, run_id=run)
    assert len(both) == 1


def test_query_limit_returns_most_recent() -> None:
    log = AuditLog()
    for i in range(5):
        log.record(category=AuditCategory.AGENT_ACTION, action=f"step-{i}")
    recent = log.query(limit=2)
    assert len(recent) == 2
    assert [e.action for e in recent] == ["step-3", "step-4"]


def test_clean_chain_verifies() -> None:
    log = AuditLog()
    for i in range(4):
        log.record(category=AuditCategory.AGENT_ACTION, action=f"a{i}")
    assert log.verify_integrity() is True
    assert verify_chain(log.store.all()) is True


def test_tampered_entry_breaks_chain_verification() -> None:
    log = AuditLog()
    for i in range(3):
        log.record(category=AuditCategory.AGENT_ACTION, action=f"a{i}")
    entries = log.store.all()
    # Forge a record that changes content but keeps the original hash.
    tampered = entries[1].model_copy(update={"action": "MALICIOUS"})
    chain = [entries[0], tampered, entries[2]]
    assert verify_chain(chain) is False


def test_entries_are_hash_chained_to_predecessor() -> None:
    log = AuditLog()
    first = log.record(category=AuditCategory.AGENT_ACTION, action="a0")
    second = log.record(category=AuditCategory.AGENT_ACTION, action="a1")
    assert first.entry_hash
    assert second.prev_hash == first.entry_hash


def test_audit_entry_default_construction_is_redacted_flag_true() -> None:
    entry = AuditEntry(category=AuditCategory.SYSTEM, action="boot")
    assert entry.redacted is True
