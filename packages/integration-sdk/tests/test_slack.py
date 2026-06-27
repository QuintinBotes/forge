"""Tests for the Slack notifier (plan Task 1.13).

Delivery goes through ``httpx.MockTransport`` (no live Slack). Asserts approval
messages are formatted with the spec's "Approval UI Must Show" essentials and an
Approve / Reject / Request changes action set.
"""

from __future__ import annotations

import json
import uuid

import httpx
from conftest import RequestRecorder, make_transport

from forge_contracts import (
    ApprovalGate,
    ApprovalRequest,
    CheckResult,
    SlackDeliveryResult,
    SlackMessage,
)
from forge_integrations import SlackNotifier


def _ok_handler(rec: RequestRecorder):
    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return httpx.Response(
            200, json={"ok": True, "channel": "C123", "ts": "1700000000.000100"}
        )

    return handler


def test_notify_posts_message_and_parses_result() -> None:
    rec = RequestRecorder()
    notifier = SlackNotifier(token="xoxb-test", transport=make_transport(_ok_handler(rec)))
    result = notifier.notify(SlackMessage(channel="#general", text="hello"))

    assert isinstance(result, SlackDeliveryResult)
    assert result.ok is True
    assert result.channel == "C123"
    assert result.ts == "1700000000.000100"
    assert rec.last.url.path.endswith("/chat.postMessage")
    assert rec.last.headers["authorization"] == "Bearer xoxb-test"
    body = json.loads(rec.last.content)
    assert body["channel"] == "#general"
    assert body["text"] == "hello"


def test_notify_includes_blocks_and_thread() -> None:
    rec = RequestRecorder()
    notifier = SlackNotifier(token="t", transport=make_transport(_ok_handler(rec)))
    notifier.notify(
        SlackMessage(
            channel="#c",
            text="t",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
            thread_ts="123.456",
        )
    )
    body = json.loads(rec.last.content)
    assert body["blocks"]
    assert body["thread_ts"] == "123.456"


def test_notify_reports_slack_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    notifier = SlackNotifier(token="t", transport=make_transport(handler))
    result = notifier.notify(SlackMessage(channel="#nope", text="x"))
    assert result.ok is False
    assert result.error == "channel_not_found"


def test_build_approval_message_contains_required_fields() -> None:
    notifier = SlackNotifier(token="t", default_channel="#approvals")
    request = ApprovalRequest(
        id=uuid.uuid4(),
        gate=ApprovalGate.PR,
        task_id=uuid.uuid4(),
        title="Add customer search endpoint",
        summary="Implements SPEC-17 acceptance criteria A1, A2.",
        changed_files=["app/main.py", "app/search.py", "tests/test_search.py"],
        verification=[
            CheckResult(name="lint", passed=True),
            CheckResult(name="tests", passed=True),
            CheckResult(name="coverage", passed=False, details="78% < 80%"),
        ],
        confidence=0.81,
        risks=["coverage below threshold"],
    )
    msg = notifier.build_approval_message(request)
    assert isinstance(msg, SlackMessage)
    assert msg.channel == "#approvals"

    text = msg.text
    assert "pr" in text.lower()
    assert "Add customer search endpoint" in text
    assert "3" in text  # changed file count
    assert "2/3" in text  # verification summary
    assert "0.81" in text
    assert "coverage below threshold" in text

    # Action buttons: Approve / Reject / Request changes.
    serialized = json.dumps(msg.blocks)
    assert "Approve" in serialized
    assert "Reject" in serialized
    assert "Request changes" in serialized
    # The approval id is embedded so the interaction handler can resolve it.
    assert str(request.id) in serialized


def test_notify_approval_sends_built_message() -> None:
    rec = RequestRecorder()
    notifier = SlackNotifier(
        token="t", default_channel="#approvals", transport=make_transport(_ok_handler(rec))
    )
    request = ApprovalRequest(id=uuid.uuid4(), gate=ApprovalGate.DEPLOY, title="Deploy v2")
    result = notifier.notify_approval(request)
    assert result.ok is True
    body = json.loads(rec.last.content)
    assert body["channel"] == "#approvals"
    assert "Deploy v2" in body["text"]
    assert body["blocks"]


def test_notify_approval_prefers_payload_channel() -> None:
    rec = RequestRecorder()
    notifier = SlackNotifier(token="t", transport=make_transport(_ok_handler(rec)))
    request = ApprovalRequest(
        id=uuid.uuid4(),
        gate=ApprovalGate.SPEC,
        title="Spec ready",
        payload={"slack_channel": "#team-spec"},
    )
    notifier.notify_approval(request)
    assert json.loads(rec.last.content)["channel"] == "#team-spec"


def test_health_uses_auth_test() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth.test")
        return httpx.Response(200, json={"ok": True, "team": "Forge"})

    notifier = SlackNotifier(token="t", transport=make_transport(handler))
    health = notifier.health()
    assert health.healthy is True


def test_health_unhealthy_when_not_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    notifier = SlackNotifier(token="t", transport=make_transport(handler))
    assert notifier.health().healthy is False


def test_notify_approval_without_channel_falls_back() -> None:
    rec = RequestRecorder()
    notifier = SlackNotifier(token="t", transport=make_transport(_ok_handler(rec)))
    request = ApprovalRequest(id=uuid.uuid4(), gate=ApprovalGate.PR, title="x")
    notifier.notify_approval(request)
    # A deterministic fallback channel is used rather than crashing.
    assert json.loads(rec.last.content)["channel"]
