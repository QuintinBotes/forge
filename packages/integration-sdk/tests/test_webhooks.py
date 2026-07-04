"""Tests for GitHub/Slack webhook parsing + signature verification (Task 1.13).

The CI webhook parser maps provider payloads onto the frozen ``CIStatus`` DTO.
Signature helpers are pure HMAC — no network.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from conftest import load_fixture

from forge_contracts import CIState, CIStatus, WebhookEvent
from forge_integrations import (
    parse_github_webhook,
    parse_slack_interaction,
    sign_github_payload,
    sign_slack_payload,
    verify_github_signature,
    verify_slack_signature,
)


def _event(event_type: str, fixture: str) -> WebhookEvent:
    return WebhookEvent(source="github", event_type=event_type, payload=load_fixture(fixture))


def test_parse_status_event_success() -> None:
    status = parse_github_webhook(_event("status", "webhook_status_success"))
    assert isinstance(status, CIStatus)
    assert status.repo == "org/api"
    assert status.sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert status.state is CIState.SUCCESS
    assert status.context == "ci/forge"
    assert status.description == "All checks passed"
    assert status.target_url == "https://ci.example.com/builds/100"


def test_parse_check_run_failure() -> None:
    status = parse_github_webhook(_event("check_run", "webhook_check_run_failure"))
    assert status.state is CIState.FAILURE
    assert status.sha == "cccccccccccccccccccccccccccccccccccccccc"
    assert status.context == "pytest"
    assert status.checks
    assert status.checks[0].name == "pytest"
    assert status.checks[0].passed is False


def test_parse_workflow_run_in_progress_is_pending() -> None:
    status = parse_github_webhook(_event("workflow_run", "webhook_workflow_run_pending"))
    assert status.state is CIState.PENDING
    assert status.sha == "dddddddddddddddddddddddddddddddddddddddd"
    assert status.context == "CI"


@pytest.mark.parametrize(
    ("status_value", "conclusion", "expected"),
    [
        ("completed", "success", CIState.SUCCESS),
        ("completed", "neutral", CIState.SUCCESS),
        ("completed", "skipped", CIState.SUCCESS),
        ("completed", "failure", CIState.FAILURE),
        ("completed", "timed_out", CIState.FAILURE),
        ("completed", "cancelled", CIState.ERROR),
        ("completed", "action_required", CIState.ERROR),
        ("in_progress", None, CIState.PENDING),
        ("queued", None, CIState.PENDING),
    ],
)
def test_check_conclusion_mapping(status_value, conclusion, expected) -> None:
    event = WebhookEvent(
        source="github",
        event_type="check_run",
        payload={
            "check_run": {
                "name": "x",
                "status": status_value,
                "conclusion": conclusion,
                "head_sha": "z",
            },
            "repository": {"full_name": "org/api"},
        },
    )
    assert parse_github_webhook(event).state is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("success", CIState.SUCCESS),
        ("failure", CIState.FAILURE),
        ("error", CIState.ERROR),
        ("pending", CIState.PENDING),
        ("weird-unknown", CIState.PENDING),
    ],
)
def test_status_state_mapping(state, expected) -> None:
    event = WebhookEvent(
        source="github",
        event_type="status",
        payload={"sha": "s", "state": state, "repository": {"full_name": "o/r"}},
    )
    assert parse_github_webhook(event).state is expected


def test_unknown_event_type_is_pending() -> None:
    event = WebhookEvent(
        source="github",
        event_type="push",
        payload={"after": "s", "repository": {"full_name": "o/r"}},
    )
    status = parse_github_webhook(event)
    assert status.state is CIState.PENDING


# --------------------------------------------------------------------------- #
# Signature verification                                                       #
# --------------------------------------------------------------------------- #


def test_github_signature_roundtrip() -> None:
    secret = "topsecret"
    body = b'{"action":"completed"}'
    sig = sign_github_payload(secret, body)
    assert sig.startswith("sha256=")
    assert verify_github_signature(secret, body, sig) is True


def test_github_signature_rejects_tampered_body() -> None:
    secret = "topsecret"
    sig = sign_github_payload(secret, b"original")
    assert verify_github_signature(secret, b"tampered", sig) is False


def test_github_signature_rejects_missing_header() -> None:
    assert verify_github_signature("s", b"x", None) is False
    assert verify_github_signature("s", b"x", "") is False
    assert verify_github_signature("s", b"x", "md5=abc") is False


def test_slack_signature_roundtrip() -> None:
    secret = "slacksecret"
    ts = "1700000000"
    body = b"token=abc&team_id=T1"
    basestring = f"v0:{ts}:{body.decode()}".encode()
    sig = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    assert verify_slack_signature(secret, ts, body, sig, now=1700000010) is True


def test_slack_signature_rejects_stale_timestamp() -> None:
    secret = "slacksecret"
    ts = "1700000000"
    body = b"x"
    basestring = f"v0:{ts}:{body.decode()}".encode()
    sig = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    # now is more than 5 minutes after ts -> replay rejected.
    assert verify_slack_signature(secret, ts, body, sig, now=1700001000) is False


def test_slack_signature_rejects_bad_signature() -> None:
    assert verify_slack_signature("s", "1700000000", b"x", "v0=deadbeef", now=1700000000) is False
    assert verify_slack_signature("s", "not-a-number", b"x", "v0=x", now=1) is False


def test_signatures_are_constant_time_comparable() -> None:
    # Defensive: ensure we used hmac.compare_digest semantics (no length leak crash).
    secret = "s"
    body = json.dumps({"a": 1}).encode()
    sig = sign_github_payload(secret, body)
    assert verify_github_signature(secret, body, sig[:-1] + "0") is False


# --------------------------------------------------------------------------- #
# HARD-06: Slack v0 sign/verify round-trip + interaction parsing (AC3)         #
# --------------------------------------------------------------------------- #


def test_sign_then_verify_roundtrips() -> None:
    secret = "slack-signing-secret"
    ts = "1700000000"
    body = b"token=abc&command=%2Fforge&text=help"
    sig = sign_slack_payload(secret, ts, body)
    assert sig.startswith("v0=")
    assert verify_slack_signature(secret, ts, body, sig, now=1700000010) is True
    # str body signs identically to the equivalent bytes body.
    assert sign_slack_payload(secret, ts, body.decode()) == sig


def test_verify_rejects_wrong_secret_tampered_missing_and_stale() -> None:
    secret = "right-secret"
    ts = "1700000000"
    body = b"payload=%7B%22a%22%3A1%7D"
    good = sign_slack_payload(secret, ts, body)

    # (a) wrong secret
    wrong = sign_slack_payload("wrong-secret", ts, body)
    assert verify_slack_signature(secret, ts, body, wrong, now=1700000010) is False
    # (b) tampered body
    assert verify_slack_signature(secret, ts, b"payload=tampered", good, now=1700000010) is False
    # (c) missing signature
    assert verify_slack_signature(secret, ts, body, None, now=1700000010) is False
    assert verify_slack_signature(secret, ts, body, "", now=1700000010) is False
    # (d) stale timestamp (older than the 300s replay window)
    assert verify_slack_signature(secret, ts, body, good, now=1700000000 + 301) is False
    # ... and a valid, in-window request still verifies.
    assert verify_slack_signature(secret, ts, body, good, now=1700000000 + 299) is True


def test_verify_rejects_future_skew() -> None:
    secret = "s"
    ts = "1700000000"
    body = b"x"
    sig = sign_slack_payload(secret, ts, body)
    # A timestamp far in the future is also rejected (symmetric skew guard).
    assert verify_slack_signature(secret, ts, body, sig, now=1700000000 - 400) is False


def test_parse_slack_interaction_decodes_payload() -> None:
    payload = json.dumps({"type": "block_actions", "actions": [{"value": "approve:abc"}]})
    parsed = parse_slack_interaction(payload)
    assert parsed["type"] == "block_actions"
    assert parsed["actions"][0]["value"] == "approve:abc"


def test_parse_slack_interaction_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        parse_slack_interaction("}{not json")
    # A JSON scalar (not an object) is also rejected.
    with pytest.raises(ValueError):
        parse_slack_interaction("42")
