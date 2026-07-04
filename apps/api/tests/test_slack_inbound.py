"""HARD-06 — inbound Slack routes (signed slash command + interactivity).

The two inbound routes (``/integration/slack/commands`` +
``/integration/slack/interactions``) are **signature-verified untrusted intake**
(no principal dep): the Slack v0 signature over the raw body is the only trust
boundary. They fail closed — 501 when the signing secret is unconfigured, 401 on
a bad/missing/stale signature — and an interactive Approve/Reject payload
round-trips a decision into the workspace-scoped :class:`ApprovalStore`.

All offline (no live Slack): the notifier is a duck-typed fake and signatures are
built with the real ``sign_slack_payload`` so the verifier's algorithm is
exercised end-to-end.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.routers.approval import ApprovalStore, get_approval_store
from forge_api.routers.integration import (
    SlackApprovalRefStore,
    get_integration_audit_log,
    get_slack_notifier,
    get_slack_ref_store,
    get_slack_signing_secret,
)
from forge_contracts import ApprovalGate, ApprovalRequest, SlackDeliveryResult, UserRole
from forge_contracts.enums import ApprovalStatus
from forge_integrations import sign_slack_payload

SIGNING_SECRET = "test-slack-signing-secret-0123456789abcdef"
# Self-contained principal (avoids importing the shadowed bare ``conftest`` name
# across the full-suite run, where a nested tests/sso/conftest.py collides).
WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c6")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d7")


def _principal() -> Principal:
    return Principal(
        user_id=USER_ID,
        workspace_id=WS_ID,
        role=UserRole.ADMIN,
        email="slack-test@forge.local",
        auth_method="test",
        scopes=["*"],
    )


class FakeNotifier:
    """Duck-typed Slack notifier that records ``update_message`` calls (no network)."""

    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []

    def update_message(
        self, *, channel: str, ts: str, text: str, blocks=None
    ) -> SlackDeliveryResult:
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return SlackDeliveryResult(ok=True, channel=channel, ts=ts)


def _signed(secret: str, body: bytes, *, skew: int = 0) -> dict[str, str]:
    """Build Slack v0 signature headers against the real wall clock."""
    ts = str(int(time.time()) + skew)
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sign_slack_payload(secret, ts, body),
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _build_app(
    authenticate_app: Callable[..., FastAPI],
    *,
    secret: str | None = SIGNING_SECRET,
    store: ApprovalStore | None = None,
    notifier: object | None = None,
    audit: AuditLog | None = None,
    refs: SlackApprovalRefStore | None = None,
) -> FastAPI:
    app = create_app()
    authenticate_app(app, _principal())
    app.dependency_overrides[get_slack_signing_secret] = lambda: secret
    if store is not None:
        app.dependency_overrides[get_approval_store] = lambda: store
    if notifier is not None:
        app.dependency_overrides[get_slack_notifier] = lambda: notifier
    if audit is not None:
        app.dependency_overrides[get_integration_audit_log] = lambda: audit
    if refs is not None:
        app.dependency_overrides[get_slack_ref_store] = lambda: refs
    return app


def _seeded_store() -> tuple[ApprovalStore, ApprovalRequest]:
    store = ApprovalStore()
    req = ApprovalRequest(
        id=uuid.uuid4(),
        gate=ApprovalGate.PR,
        title="Add customer search endpoint",
    )
    store.create(req, workspace_id=WS_ID)
    return store, req


def _block_actions_body(verb: str, approval_id: uuid.UUID, *, username: str = "reviewer") -> bytes:
    payload = {
        "type": "block_actions",
        "user": {"id": "U123", "username": username},
        "actions": [{"action_id": f"approval_{verb}", "value": f"{verb}:{approval_id}"}],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


# --------------------------------------------------------------------------- #
# AC4 — /slack/commands: 501 unset, 401 bad/stale, 200 valid help             #
# --------------------------------------------------------------------------- #


def test_commands_501_when_secret_unset(authenticate_app: Callable[..., FastAPI]) -> None:
    app = _build_app(authenticate_app, secret=None)
    body = urlencode({"command": "/forge", "text": "help"}).encode()
    with TestClient(app) as c:
        resp = c.post("/integration/slack/commands", content=body, headers=_signed("x", body))
    assert resp.status_code == 501, resp.text


def test_commands_401_on_bad_signature(authenticate_app: Callable[..., FastAPI]) -> None:
    app = _build_app(authenticate_app)
    body = urlencode({"command": "/forge", "text": "help"}).encode()
    headers = _signed("the-wrong-secret", body)
    with TestClient(app) as c:
        resp = c.post("/integration/slack/commands", content=body, headers=headers)
    assert resp.status_code == 401, resp.text


def test_commands_401_on_missing_signature(authenticate_app: Callable[..., FastAPI]) -> None:
    app = _build_app(authenticate_app)
    body = urlencode({"command": "/forge", "text": "help"}).encode()
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/commands",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 401, resp.text


def test_commands_401_on_stale_timestamp(authenticate_app: Callable[..., FastAPI]) -> None:
    app = _build_app(authenticate_app)
    body = urlencode({"command": "/forge", "text": "help"}).encode()
    headers = _signed(SIGNING_SECRET, body, skew=-400)  # older than the 300s window
    with TestClient(app) as c:
        resp = c.post("/integration/slack/commands", content=body, headers=headers)
    assert resp.status_code == 401, resp.text


def test_commands_200_help_for_valid_signed_request(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    app = _build_app(authenticate_app)
    body = urlencode({"command": "/forge", "text": "help"}).encode()
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/commands", content=body, headers=_signed(SIGNING_SECRET, body)
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    assert data["blocks"]
    assert "forge" in json.dumps(data).lower()


# --------------------------------------------------------------------------- #
# AC5 — /slack/interactions: Approve/Reject round-trip into the store          #
# --------------------------------------------------------------------------- #


def test_interaction_approve_decides_gate(authenticate_app: Callable[..., FastAPI]) -> None:
    store, req = _seeded_store()
    app = _build_app(authenticate_app, store=store, notifier=FakeNotifier())
    body = _block_actions_body("approve", req.id)  # type: ignore[arg-type]
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/interactions", content=body, headers=_signed(SIGNING_SECRET, body)
        )
    assert resp.status_code == 200, resp.text
    decided = store.get(req.id, workspace_id=WS_ID)  # type: ignore[arg-type]
    assert decided is not None
    assert decided.status is ApprovalStatus.APPROVED
    assert decided.decided_by == "slack:reviewer"


def test_interaction_reject_decides_gate(authenticate_app: Callable[..., FastAPI]) -> None:
    store, req = _seeded_store()
    app = _build_app(authenticate_app, store=store, notifier=FakeNotifier())
    body = _block_actions_body("reject", req.id, username="bob")  # type: ignore[arg-type]
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/interactions", content=body, headers=_signed(SIGNING_SECRET, body)
        )
    assert resp.status_code == 200, resp.text
    decided = store.get(req.id, workspace_id=WS_ID)  # type: ignore[arg-type]
    assert decided is not None
    assert decided.status is ApprovalStatus.REJECTED
    assert decided.decided_by == "slack:bob"


def test_interaction_501_when_secret_unset(authenticate_app: Callable[..., FastAPI]) -> None:
    store, req = _seeded_store()
    app = _build_app(authenticate_app, secret=None, store=store)
    body = _block_actions_body("approve", req.id)  # type: ignore[arg-type]
    with TestClient(app) as c:
        resp = c.post("/integration/slack/interactions", content=body, headers=_signed("x", body))
    assert resp.status_code == 501, resp.text
    # No state change on a fail-closed rejection.
    assert store.get(req.id, workspace_id=WS_ID).status is ApprovalStatus.PENDING  # type: ignore[union-attr, arg-type]


def test_interaction_401_on_bad_signature(authenticate_app: Callable[..., FastAPI]) -> None:
    store, req = _seeded_store()
    app = _build_app(authenticate_app, store=store)
    body = _block_actions_body("approve", req.id)  # type: ignore[arg-type]
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/interactions", content=body, headers=_signed("nope", body)
        )
    assert resp.status_code == 401, resp.text
    assert store.get(req.id, workspace_id=WS_ID).status is ApprovalStatus.PENDING  # type: ignore[union-attr, arg-type]


# --------------------------------------------------------------------------- #
# AC6 — unknown/cross-tenant id is a no-op 200 with an audit row              #
# --------------------------------------------------------------------------- #


def test_interaction_unknown_id_is_noop_200(authenticate_app: Callable[..., FastAPI]) -> None:
    store, _ = _seeded_store()
    audit = AuditLog()
    app = _build_app(authenticate_app, store=store, notifier=FakeNotifier(), audit=audit)
    unknown = uuid.uuid4()
    body = _block_actions_body("approve", unknown)
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/interactions", content=body, headers=_signed(SIGNING_SECRET, body)
        )
    assert resp.status_code == 200, resp.text  # 200 so Slack does not retry
    noops = [e for e in audit.query(category=AuditCategory.INTEGRATION) if "noop" in e.action]
    assert noops, "a no-op audit row is written"
    assert noops[-1].target == str(unknown)


# --------------------------------------------------------------------------- #
# AC9 — interactivity handler renders an in-place chat.update when a ref exists #
# --------------------------------------------------------------------------- #


def test_interaction_updates_original_message_when_ref_present(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    store, req = _seeded_store()
    notifier = FakeNotifier()
    refs = SlackApprovalRefStore()
    refs.record(req.id, channel="C777", ts="1700000000.001")  # type: ignore[arg-type]
    app = _build_app(authenticate_app, store=store, notifier=notifier, refs=refs)
    body = _block_actions_body("approve", req.id)  # type: ignore[arg-type]
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/interactions", content=body, headers=_signed(SIGNING_SECRET, body)
        )
    assert resp.status_code == 200, resp.text
    assert len(notifier.updates) == 1
    update = notifier.updates[0]
    assert update["channel"] == "C777"
    assert update["ts"] == "1700000000.001"
    assert "Approved by slack:reviewer" in str(update["text"])


# --------------------------------------------------------------------------- #
# AC8 — no secret/signature ever lands in an audit row                        #
# --------------------------------------------------------------------------- #


def test_no_secret_or_signature_in_audit(authenticate_app: Callable[..., FastAPI]) -> None:
    store, req = _seeded_store()
    audit = AuditLog()
    app = _build_app(authenticate_app, store=store, notifier=FakeNotifier(), audit=audit)
    body = _block_actions_body("approve", req.id)  # type: ignore[arg-type]
    headers = _signed(SIGNING_SECRET, body)
    with TestClient(app) as c:
        resp = c.post("/integration/slack/interactions", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    serialized = json.dumps(
        [e.model_dump(mode="json") for e in audit.query(category=AuditCategory.INTEGRATION)]
    )
    assert SIGNING_SECRET not in serialized
    assert headers["X-Slack-Signature"] not in serialized


# --------------------------------------------------------------------------- #
# Malformed interaction payload -> 400 (verified request, garbage body)        #
# --------------------------------------------------------------------------- #


def test_interaction_malformed_payload_is_400(authenticate_app: Callable[..., FastAPI]) -> None:
    store, _ = _seeded_store()
    app = _build_app(authenticate_app, store=store)
    body = urlencode({"payload": "}{not-json"}).encode()
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/interactions", content=body, headers=_signed(SIGNING_SECRET, body)
        )
    assert resp.status_code == 400, resp.text


def test_interaction_unrecognised_action_is_ignored_200(
    authenticate_app: Callable[..., FastAPI],
) -> None:
    store, req = _seeded_store()
    app = _build_app(authenticate_app, store=store, notifier=FakeNotifier())
    # A payload with an action verb we don't handle: no decision, still 200.
    body = _block_actions_body("frobnicate", req.id)  # type: ignore[arg-type]
    with TestClient(app) as c:
        resp = c.post(
            "/integration/slack/interactions", content=body, headers=_signed(SIGNING_SECRET, body)
        )
    assert resp.status_code == 200, resp.text
    assert store.get(req.id, workspace_id=WS_ID).status is ApprovalStatus.PENDING  # type: ignore[union-attr, arg-type]
