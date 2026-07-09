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


# --- Asana HMAC (F40) -------------------------------------------------------- #


def test_verify_asana_valid_and_tampered() -> None:
    from forge_integrations.pm.asana.webhooks import sign_asana, verify_asana

    body = json.dumps(
        {"events": [{"action": "changed", "resource": {"gid": "10001", "resource_type": "task"}}]}
    ).encode()
    sig = sign_asana(SECRET, body)
    assert verify_asana(SECRET, body, sig) is True
    assert verify_asana(SECRET, body[:-1] + b"x", sig) is False
    assert verify_asana(SECRET, body, None) is False
    assert verify_asana("", body, sig) is False


def test_parse_asana_events() -> None:
    from forge_integrations.pm.asana.webhooks import parse_asana

    for action, expected in [
        ("added", "issue.created"),
        ("changed", "issue.updated"),
        ("deleted", "issue.deleted"),
    ]:
        body = {"events": [{"action": action, "resource": {"gid": "10001"}}]}
        ev = parse_asana(body, delivery_id="a1", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "10001"


def test_parse_asana_empty_events_defaults_to_updated() -> None:
    from forge_integrations.pm.asana.webhooks import parse_asana

    ev = parse_asana({"events": []}, delivery_id="a2", signature_valid=False)
    assert ev.event_type == "issue.updated"
    assert ev.external_id is None


# --- monday.com secret header + handshake (F40) ------------------------------ #


def test_verify_monday_secret_match_mismatch() -> None:
    from forge_integrations.pm.monday.webhooks import verify_monday

    assert verify_monday(SECRET, SECRET) is True
    assert verify_monday(SECRET, "wrong") is False
    assert verify_monday(SECRET, None) is False


def test_monday_challenge_handshake() -> None:
    from forge_integrations.pm.monday.webhooks import challenge_response, is_challenge

    body = {"challenge": "abc123"}
    assert is_challenge(body) is True
    assert challenge_response(body) == {"challenge": "abc123"}
    assert is_challenge({"event": {"type": "create_pulse"}}) is False


def test_parse_monday_events() -> None:
    from forge_integrations.pm.monday.webhooks import parse_monday

    for event_type, expected in [
        ("create_pulse", "issue.created"),
        ("update_column_value", "issue.updated"),
        ("delete_pulse", "issue.deleted"),
    ]:
        body = {"event": {"type": event_type, "pulseId": 1001, "boardId": 500}}
        ev = parse_monday(body, delivery_id="m1", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "1001"


# --- GitHub Projects v2 — reuses the shared X-Hub-Signature-256 verifier ---- #


def test_verify_github_projects_reuses_shared_hmac_helper() -> None:
    from forge_integrations.pm.github_projects.webhooks import (
        sign_github_payload,
        verify_github_projects,
    )
    from forge_integrations.webhooks import sign_github_payload as shared_sign
    from forge_integrations.webhooks import verify_github_signature as shared_verify

    body = json.dumps({"action": "created", "projects_v2_item": {"node_id": "PVTI_1"}}).encode()
    sig = sign_github_payload(SECRET, body)
    assert sig == shared_sign(SECRET, body)  # identical signing, not reimplemented
    assert verify_github_projects(SECRET, body, sig) is True
    assert verify_github_projects(SECRET, body, sig) == shared_verify(SECRET, body, sig)
    assert verify_github_projects(SECRET, body[:-1] + b"x", sig) is False


def test_parse_github_projects_actions() -> None:
    from forge_integrations.pm.github_projects.webhooks import parse_github_projects

    for action, expected in [
        ("created", "issue.created"),
        ("edited", "issue.updated"),
        ("deleted", "issue.deleted"),
    ]:
        body = {
            "action": action,
            "projects_v2_item": {"node_id": "PVTI_1", "content_type": "DraftIssue"},
        }
        ev = parse_github_projects(body, delivery_id="g1", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "PVTI_1"


# --- ClickUp HMAC (F40-PM-ADAPTERS-2) --------------------------------------- #


def test_verify_clickup_valid_and_tampered() -> None:
    from forge_integrations.pm.clickup.webhooks import sign_clickup, verify_clickup

    body = json.dumps({"event": "taskUpdated", "task_id": "9hz"}).encode()
    sig = sign_clickup(SECRET, body)
    assert verify_clickup(SECRET, body, sig) is True
    assert verify_clickup(SECRET, body[:-1] + b"x", sig) is False
    assert verify_clickup(SECRET, body, None) is False
    assert verify_clickup("", body, sig) is False


def test_parse_clickup_events() -> None:
    from forge_integrations.pm.clickup.webhooks import parse_clickup

    for event, expected in [
        ("taskCreated", "issue.created"),
        ("taskUpdated", "issue.updated"),
        ("taskDeleted", "issue.deleted"),
    ]:
        body = {"event": event, "task_id": "9hz"}
        ev = parse_clickup(body, delivery_id="cu1", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "9hz"


def test_parse_clickup_falls_back_to_history_item_parent_id() -> None:
    from forge_integrations.pm.clickup.webhooks import parse_clickup

    body = {
        "event": "taskPriorityUpdated",
        "history_items": [{"parent_id": "9hz"}],
    }
    ev = parse_clickup(body, delivery_id="cu2", signature_valid=True)
    assert ev.event_type == "issue.updated"
    assert ev.external_id == "9hz"


# --- Trello HMAC-SHA1 (body + callback URL) (F40) ---------------------------- #


def test_verify_trello_valid_and_tampered() -> None:
    from forge_integrations.pm.trello.webhooks import sign_trello, verify_trello

    callback_url = "https://forge.example/webhook"
    body = json.dumps(
        {"action": {"type": "updateCard", "data": {"card": {"id": "card1"}}}}
    ).encode()
    sig = sign_trello(SECRET, body, callback_url)
    assert verify_trello(SECRET, callback_url, body, sig) is True
    assert verify_trello(SECRET, callback_url, body[:-1] + b"x", sig) is False
    assert verify_trello(SECRET, "https://other.example/webhook", body, sig) is False
    assert verify_trello(SECRET, callback_url, body, None) is False
    assert verify_trello("", callback_url, body, sig) is False


def test_parse_trello_actions() -> None:
    from forge_integrations.pm.trello.webhooks import parse_trello

    for action_type, expected in [
        ("createCard", "issue.created"),
        ("updateCard", "issue.updated"),
        ("deleteCard", "issue.deleted"),
    ]:
        body = {"action": {"type": action_type, "data": {"card": {"id": "card1"}}}}
        ev = parse_trello(body, delivery_id="t1", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "card1"


# --- GitLab shared secret token + delivery UUID (F40) ------------------------ #


def test_verify_gitlab_secret_match_mismatch() -> None:
    from forge_integrations.pm.gitlab.webhooks import verify_gitlab

    assert verify_gitlab(SECRET, SECRET) is True
    assert verify_gitlab(SECRET, "wrong") is False
    assert verify_gitlab(SECRET, None) is False


def test_parse_gitlab_actions() -> None:
    from forge_integrations.pm.gitlab.webhooks import parse_gitlab

    for action, expected in [
        ("open", "issue.created"),
        ("update", "issue.updated"),
        ("close", "issue.updated"),
        ("reopen", "issue.updated"),
    ]:
        body = {
            "object_kind": "issue",
            "object_attributes": {"iid": 42, "action": action},
        }
        ev = parse_gitlab(body, delivery_id="gl1", signature_valid=True)
        assert ev.event_type == expected
        assert ev.external_id == "42"


# --- Generic/BYO-board webhook (config-driven) (F40) ------------------------- #


def test_verify_generic_none_algo_always_true() -> None:
    from forge_contracts.pm import GenericWebhookConfig
    from forge_integrations.pm.generic.webhooks import verify_generic

    config = GenericWebhookConfig()
    assert verify_generic(config, "", b"{}", {}) is True


def test_verify_generic_shared_secret_header() -> None:
    from forge_contracts.pm import GenericWebhookConfig, GenericWebhookSignatureAlgo
    from forge_integrations.pm.generic.webhooks import verify_generic

    config = GenericWebhookConfig(
        signature_header="X-Byo-Secret",
        signature_algo=GenericWebhookSignatureAlgo.shared_secret_header,
    )
    assert verify_generic(config, SECRET, b"{}", {"X-Byo-Secret": SECRET}) is True
    assert verify_generic(config, SECRET, b"{}", {"X-Byo-Secret": "wrong"}) is False
    assert verify_generic(config, SECRET, b"{}", {}) is False


def test_verify_generic_hmac_sha256_hex() -> None:
    import hashlib
    import hmac

    from forge_contracts.pm import GenericWebhookConfig, GenericWebhookSignatureAlgo
    from forge_integrations.pm.generic.webhooks import verify_generic

    body = b'{"type":"ticket_updated"}'
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    config = GenericWebhookConfig(
        signature_header="X-Byo-Signature",
        signature_algo=GenericWebhookSignatureAlgo.hmac_sha256_hex,
    )
    assert verify_generic(config, SECRET, body, {"X-Byo-Signature": sig}) is True
    assert verify_generic(config, SECRET, body[:-1] + b"x", {"X-Byo-Signature": sig}) is False


def test_verify_generic_hmac_sha1_base64() -> None:
    import base64
    import hashlib
    import hmac

    from forge_contracts.pm import GenericWebhookConfig, GenericWebhookSignatureAlgo
    from forge_integrations.pm.generic.webhooks import verify_generic

    body = b'{"type":"ticket_updated"}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha1).digest()
    sig = base64.b64encode(digest).decode()
    config = GenericWebhookConfig(
        signature_header="X-Byo-Signature",
        signature_algo=GenericWebhookSignatureAlgo.hmac_sha1_base64,
    )
    assert verify_generic(config, SECRET, body, {"X-Byo-Signature": sig}) is True
    assert verify_generic(config, SECRET, body[:-1] + b"x", {"X-Byo-Signature": sig}) is False


def test_parse_generic_maps_configured_event_types() -> None:
    from forge_contracts.pm import GenericWebhookConfig
    from forge_integrations.pm.generic.webhooks import parse_generic

    config = GenericWebhookConfig(
        event_type_path="type",
        external_id_path="ticket.id",
        event_type_map={
            "ticket_created": "issue.created",
            "ticket_updated": "issue.updated",
            "ticket_deleted": "issue.deleted",
        },
        default_event_type="issue.updated",
    )
    body = {"type": "ticket_created", "ticket": {"id": "t-1"}}
    ev = parse_generic(config, body, delivery_id="byo1", signature_valid=True)
    assert ev.event_type == "issue.created"
    assert ev.external_id == "t-1"


def test_parse_generic_falls_back_to_default_event_type() -> None:
    from forge_contracts.pm import GenericWebhookConfig
    from forge_integrations.pm.generic.webhooks import parse_generic

    config = GenericWebhookConfig(event_type_path="type", external_id_path="ticket.id")
    body = {"type": "something_unrecognized", "ticket": {"id": "t-1"}}
    ev = parse_generic(config, body, delivery_id="byo2", signature_valid=False)
    assert ev.event_type == "issue.updated"
