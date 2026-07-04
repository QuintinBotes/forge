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
        return httpx.Response(200, json={"ok": True, "channel": "C123", "ts": "1700000000.000100"})

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


# --------------------------------------------------------------------------- #
# HARD-06: bounded, rate-limit-aware retries (AC7)                             #
# --------------------------------------------------------------------------- #


class _SleepSpy:
    """Records every delay passed to the injected ``sleep`` (no real wait)."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _sequence_handler(responses: list[httpx.Response], rec: RequestRecorder):
    """Return each queued response in order (last one repeats if exhausted)."""
    box = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        idx = min(box["i"], len(responses) - 1)
        box["i"] += 1
        return responses[idx]

    return handler


def test_notify_retries_on_429_then_succeeds() -> None:
    rec = RequestRecorder()
    sleep = _SleepSpy()
    responses = [
        httpx.Response(429, headers={"Retry-After": "1"}, json={"ok": False}),
        httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"}),
    ]
    notifier = SlackNotifier(
        token="t", transport=make_transport(_sequence_handler(responses, rec)), sleep=sleep
    )
    result = notifier.notify(SlackMessage(channel="#c", text="hi"))
    assert result.ok is True
    assert len(rec.requests) == 2  # exactly one retry
    assert sleep.delays == [1.0]  # honoured Retry-After


def test_notify_respects_retry_after_header_capped() -> None:
    rec = RequestRecorder()
    sleep = _SleepSpy()
    responses = [
        httpx.Response(429, headers={"Retry-After": "999"}, json={"ok": False}),
        httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"}),
    ]
    notifier = SlackNotifier(
        token="t",
        transport=make_transport(_sequence_handler(responses, rec)),
        sleep=sleep,
        retry_cap_seconds=5.0,
    )
    notifier.notify(SlackMessage(channel="#c", text="hi"))
    assert sleep.delays == [5.0]  # Retry-After capped at retry_cap_seconds


def test_notify_gives_up_after_max_retries_returns_ok_false() -> None:
    rec = RequestRecorder()
    sleep = _SleepSpy()
    responses = [httpx.Response(429, headers={"Retry-After": "0"}, json={"ok": False})]
    notifier = SlackNotifier(
        token="t",
        transport=make_transport(_sequence_handler(responses, rec)),
        sleep=sleep,
        max_retries=2,
    )
    result = notifier.notify(SlackMessage(channel="#c", text="hi"))
    assert result.ok is False  # never raises
    assert result.error == "http 429"
    assert len(rec.requests) == 3  # initial + 2 retries


def test_notify_retries_on_5xx_then_gives_up() -> None:
    rec = RequestRecorder()
    sleep = _SleepSpy()
    responses = [httpx.Response(503, text="unavailable")]
    notifier = SlackNotifier(
        token="t",
        transport=make_transport(_sequence_handler(responses, rec)),
        sleep=sleep,
        max_retries=1,
        retry_base_delay=0.5,
    )
    result = notifier.notify(SlackMessage(channel="#c", text="hi"))
    assert result.ok is False
    assert result.error == "http 503"
    assert len(rec.requests) == 2  # initial + 1 retry
    assert sleep.delays == [0.5]  # base * 2**0


def test_notify_treats_200_rate_limited_body_like_429() -> None:
    rec = RequestRecorder()
    sleep = _SleepSpy()
    responses = [
        httpx.Response(
            200, headers={"Retry-After": "2"}, json={"ok": False, "error": "rate_limited"}
        ),
        httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "9.9"}),
    ]
    notifier = SlackNotifier(
        token="t", transport=make_transport(_sequence_handler(responses, rec)), sleep=sleep
    )
    result = notifier.notify(SlackMessage(channel="#c", text="hi"))
    assert result.ok is True
    assert sleep.delays == [2.0]
    assert len(rec.requests) == 2


def test_notify_does_not_retry_terminal_4xx() -> None:
    rec = RequestRecorder()
    sleep = _SleepSpy()
    responses = [httpx.Response(200, json={"ok": False, "error": "channel_not_found"})]
    notifier = SlackNotifier(
        token="t", transport=make_transport(_sequence_handler(responses, rec)), sleep=sleep
    )
    result = notifier.notify(SlackMessage(channel="#nope", text="hi"))
    assert result.ok is False
    assert result.error == "channel_not_found"
    assert len(rec.requests) == 1  # a non-rate-limit error is not retried
    assert sleep.delays == []


# --------------------------------------------------------------------------- #
# HARD-06: chat.update (AC9)                                                    #
# --------------------------------------------------------------------------- #


def test_update_message_calls_chat_update_with_ts() -> None:
    rec = RequestRecorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.record(request)
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1700.1"})

    notifier = SlackNotifier(token="t", transport=make_transport(handler))
    result = notifier.update_message(
        channel="C1",
        ts="1700.1",
        text="Approved by @reviewer",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "Approved"}}],
    )
    assert result.ok is True
    assert rec.last.url.path.endswith("/chat.update")
    body = json.loads(rec.last.content)
    assert body["channel"] == "C1"
    assert body["ts"] == "1700.1"
    assert body["text"] == "Approved by @reviewer"
    assert body["blocks"]


# --------------------------------------------------------------------------- #
# HARD-06: secret hygiene + frozen-protocol conformance (AC8, AC10)            #
# --------------------------------------------------------------------------- #


def test_secret_never_in_repr_or_str() -> None:
    notifier = SlackNotifier(token="xoxb-super-secret-token", default_channel="#a")
    for rendered in (repr(notifier), str(notifier)):
        assert "xoxb-super-secret-token" not in rendered
    assert "configured=True" in repr(notifier)


def test_slacknotifier_satisfies_frozen_protocol() -> None:
    from forge_contracts import SlackNotifier as SlackNotifierProtocol

    notifier = SlackNotifier(token="t")
    # The concrete client still structurally satisfies the frozen Protocol.
    assert isinstance(notifier, SlackNotifierProtocol)
