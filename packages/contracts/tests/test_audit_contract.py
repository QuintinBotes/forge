"""F39 canonical audit contract: hashing determinism + DTO compatibility.

Covers AC2/AC3 at the pure-contract level (the DB writer/verifier tests in
``packages/db/tests/audit`` exercise the same helpers over persisted rows) and
guards the F30/F37 producer compatibility: every pre-F39 ``AuditEvent``
construction keeps working unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from forge_contracts.audit import (
    GENESIS_HASH,
    ActorType,
    AuditAction,
    AuditEntry,
    AuditEvent,
    AuditOutcome,
    AuditResourceType,
    AuditSeverity,
    AuditSink,
    canonical_json,
    compute_entry_hash,
    compute_payload_hash,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
NOW = datetime(2026, 7, 1, 12, 0, 0, 123456, tzinfo=UTC)


def _entry_hash(**overrides) -> str:
    kwargs = {
        "prev_hash": GENESIS_HASH,
        "workspace_id": WS,
        "seq": 1,
        "occurred_at": NOW,
        "actor_type": "user",
        "actor_id": None,
        "actor_label": "user:alice@acme",
        "action": "approval.decided",
        "target_type": "pull_request",
        "target_id": None,
        "scope_type": None,
        "scope_id": None,
        "result": "success",
        "payload_hash": compute_payload_hash({"decision": "approved"}),
    }
    kwargs.update(overrides)
    return compute_entry_hash(**kwargs)


def test_canonical_json_is_stable() -> None:
    a = canonical_json({"b": 1, "a": {"y": 2, "x": [1, 2]}})
    b = canonical_json({"a": {"x": [1, 2], "y": 2}, "b": 1})
    assert a == b
    assert " " not in a  # no whitespace: byte-stable across serializers


def test_payload_hash_deterministic_and_sensitive() -> None:
    assert compute_payload_hash({"k": "v"}) == compute_payload_hash({"k": "v"})
    assert compute_payload_hash({"k": "v"}) != compute_payload_hash({"k": "w"})
    assert len(compute_payload_hash({})) == 64


def test_entry_hash_links_and_is_deterministic() -> None:
    first = _entry_hash()
    assert first == _entry_hash()  # deterministic
    # Changing any linked field changes the hash.
    assert first != _entry_hash(prev_hash="1" * 64)
    assert first != _entry_hash(seq=2)
    assert first != _entry_hash(result="denied")
    assert first != _entry_hash(action="approval.requested")
    assert first != _entry_hash(payload_hash=compute_payload_hash({"decision": "denied"}))


def test_entry_hash_is_timezone_normalized() -> None:
    """Naive-UTC (SQLite read-back) and aware-UTC (write time / Postgres)
    datetimes of the same instant hash identically — no false chain breaks."""
    naive = NOW.replace(tzinfo=None)
    assert _entry_hash(occurred_at=NOW) == _entry_hash(occurred_at=naive)


def test_genesis_prev_hash_shape() -> None:
    assert GENESIS_HASH == "0" * 64


def test_audit_event_foundation_construction_still_works() -> None:
    """The exact pre-F39 (F30/F37) constructor shape must keep validating."""
    event = AuditEvent(
        workspace_id=WS,
        action="role_grant.created",
        actor_id=uuid.uuid4(),
        actor_type="user",
        target_type="role_grant",
        target_id=uuid.uuid4(),
        scope_type="workspace",
        scope_id=WS,
        before=None,
        after={"role": "member"},
        result="success",
        details={"note": "seed"},
    )
    assert event.severity == AuditSeverity.INFO.value
    assert event.actor_label is None
    assert event.detail_ref is None
    assert event.request_id is None


def test_audit_entry_extends_event_with_chain_fields() -> None:
    entry = AuditEntry(
        id=uuid.uuid4(),
        workspace_id=WS,
        action=AuditAction.AUDIT_EXPORTED.value,
        created_at=NOW,
        seq=7,
        payload_hash="a" * 64,
        prev_hash=GENESIS_HASH,
        entry_hash="b" * 64,
    )
    assert entry.seq == 7
    assert entry.prev_hash == GENESIS_HASH


def test_vocabulary_enums_are_strs() -> None:
    # StrEnum members must be usable anywhere a plain str action/type is.
    assert AuditAction.TOOL_CALL == "tool.call"
    assert ActorType.AGENT_RUNNER == "agent_runner"
    assert AuditOutcome.BLOCKED == "blocked"
    assert AuditResourceType.PULL_REQUEST == "pull_request"
    assert AuditSeverity.CRITICAL == "critical"


def test_audit_sink_protocol_matches_foundation_sinks() -> None:
    class _Sink:
        def emit(self, event: AuditEvent) -> None:  # pragma: no cover - shape only
            del event

    assert isinstance(_Sink(), AuditSink)
