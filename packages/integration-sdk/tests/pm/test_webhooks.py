"""Unit tests for webhook verification + parsing (AC15, AC16, AC9/10/17)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from forge_integrations.pm.jira.webhooks import (
    parse_jira,
    synthesize_delivery_id,
    verify_jira,
)
from forge_integrations.pm.linear.webhooks import (
    parse_linear,
    sign_linear,
    verify_linear,
)

SECRET = "shh-very-secret"


# --- Linear HMAC (AC15) ----------------------------------------------------- #


def _linear_body(ts_ms: int) -> bytes:
    return json.dumps(
        {
            "action": "update",
            "type": "Issue",
            "webhookTimestamp": ts_ms,
            "data": {"id": "uuid-1", "identifier": "ENG-1"},
        }
    ).encode()


def test_verify_linear_valid() -> None:
    now = datetime.now(UTC).timestamp()
    body = _linear_body(int(now * 1000))
    sig = sign_linear(SECRET, body)
    assert verify_linear(SECRET, body, sig, timestamp_ms=int(now * 1000), now=now)


def test_verify_linear_missing_signature() -> None:
    body = _linear_body(int(datetime.now(UTC).timestamp() * 1000))
    assert verify_linear(SECRET, body, None) is False


def test_verify_linear_tampered_body() -> None:
    now = datetime.now(UTC).timestamp()
    body = _linear_body(int(now * 1000))
    sig = sign_linear(SECRET, body)
    tampered = body[:-2] + b"X}"
    assert verify_linear(SECRET, tampered, sig, timestamp_ms=int(now * 1000), now=now) is False


def test_verify_linear_rejects_stale_timestamp() -> None:
    now = datetime.now(UTC).timestamp()
    stale_ms = int((now - 600) * 1000)  # 10 minutes old
    body = _linear_body(stale_ms)
    sig = sign_linear(SECRET, body)
    assert (
        verify_linear(SECRET, body, sig, timestamp_ms=stale_ms, tolerance_seconds=60, now=now)
        is False
    )


def test_verify_linear_wrong_secret() -> None:
    now = datetime.now(UTC).timestamp()
    body = _linear_body(int(now * 1000))
    sig = sign_linear(SECRET, body)
    assert verify_linear("other", body, sig, timestamp_ms=int(now * 1000), now=now) is False


# --- Jira secret (AC16) ----------------------------------------------------- #


def test_verify_jira_secret_match_mismatch() -> None:
    assert verify_jira(SECRET, SECRET) is True
    assert verify_jira(SECRET, "wrong") is False
    assert verify_jira(SECRET, None) is False
    assert verify_jira("", "") is False


def test_synthesize_delivery_id_is_stable_per_minute() -> None:
    body = b'{"a":1}'
    at = datetime(2026, 1, 1, 10, 30, 0, tzinfo=UTC)
    assert synthesize_delivery_id(body, received_at=at) == synthesize_delivery_id(
        body, received_at=at
    )
    later = datetime(2026, 1, 1, 10, 31, 0, tzinfo=UTC)
    assert synthesize_delivery_id(body, received_at=at) != synthesize_delivery_id(
        body, received_at=later
    )


# --- Parsing (AC9, AC10, AC17) --------------------------------------------- #


def test_parse_jira_created_updated_deleted() -> None:
    for raw, expected in [
        ("jira:issue_created", "issue.created"),
        ("jira:issue_updated", "issue.updated"),
        ("jira:issue_deleted", "issue.deleted"),
    ]:
        body = {
            "webhookEvent": raw,
            "timestamp": 1767348000000,
            "issue": {"id": "10001", "key": "ENG-1"},
        }
        ev = parse_jira(body, delivery_id="d1", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "10001"
        assert ev.external_key == "ENG-1"
        assert ev.signature_valid is True


def test_parse_linear_actions() -> None:
    for action, expected in [
        ("create", "issue.created"),
        ("update", "issue.updated"),
        ("remove", "issue.deleted"),
    ]:
        body = {
            "action": action,
            "type": "Issue",
            "webhookTimestamp": 1767348000000,
            "data": {"id": "uuid-1", "identifier": "ENG-1"},
        }
        ev = parse_linear(body, delivery_id="d2", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "uuid-1"
        assert ev.external_key == "ENG-1"
