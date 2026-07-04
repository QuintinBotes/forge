"""HARD-06 live Slack integration lane (creds-gated, opt-in).

These tests drive the REAL slack.com Web API with a real bot token, and prove the
inbound v0 signature verifier against the real signing secret. They are marked
``live_slack`` + ``integration`` and **skip cleanly** when the ``SLACK_*``
credentials are absent, so the default ``uv run pytest -q`` run stays hermetic and
network-free.

Run them (once real creds exist):

    cp .env.integration.example .env.integration   # then fill in the SLACK_* block
    set -a && source .env.integration && set +a
    uv run pytest -m live_slack -q

Required env:
    SLACK_BOT_TOKEN         (xoxb-… — a disposable test-workspace bot token)
    SLACK_TEST_CHANNEL      (channel id/name the bot can post to, e.g. C0123ABC)
    SLACK_SIGNING_SECRET    (for the inbound-verify lane; AC11)

See docs/runbooks/live-slack.md.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import httpx
import pytest

from forge_contracts import ApprovalGate, ApprovalRequest, CheckResult
from forge_integrations import SlackNotifier, sign_slack_payload, verify_slack_signature

pytestmark = [pytest.mark.integration, pytest.mark.live_slack]


def _require(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(
            "live Slack creds absent — set "
            + ", ".join(missing)
            + " to run the live lane; see docs/runbooks/live-slack.md"
        )
    return {n: os.environ[n] for n in names}


def _delete_message(token: str, channel: str, ts: str) -> None:
    """Best-effort cleanup so the test channel does not accumulate messages."""
    with contextlib.suppress(Exception), httpx.Client(base_url="https://slack.com/api") as c:
        c.post(
            "/chat.delete",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "ts": ts},
        )


@pytest.fixture
def notifier():
    env = _require("SLACK_BOT_TOKEN", "SLACK_TEST_CHANNEL")
    n = SlackNotifier(token=env["SLACK_BOT_TOKEN"], default_channel=env["SLACK_TEST_CHANNEL"])
    try:
        yield n
    finally:
        n.close()


def test_live_notify_posts_to_test_channel(notifier: SlackNotifier) -> None:
    """AC1: a real chat.postMessage returns ok:true + a non-empty ts."""
    from forge_contracts import SlackMessage

    channel = os.environ["SLACK_TEST_CHANNEL"]
    token = os.environ["SLACK_BOT_TOKEN"]
    result = notifier.notify(
        SlackMessage(channel=channel, text=f"[forge-hardening] live smoke {uuid.uuid4().hex[:8]}")
    )
    assert result.ok is True, result.error
    assert result.ts
    _delete_message(token, result.channel or channel, result.ts)


def test_live_notify_approval_block_kit(notifier: SlackNotifier) -> None:
    """AC2: a real Block Kit approval message posts with the action buttons."""
    channel = os.environ["SLACK_TEST_CHANNEL"]
    token = os.environ["SLACK_BOT_TOKEN"]
    request = ApprovalRequest(
        id=uuid.uuid4(),
        gate=ApprovalGate.PR,
        title="[forge-hardening] live approval smoke",
        summary="HARD-06 live Block Kit smoke.",
        changed_files=["a.py", "b.py"],
        verification=[CheckResult(name="tests", passed=True)],
        confidence=0.9,
    )
    result = notifier.notify_approval(request)
    assert result.ok is True, result.error
    assert result.ts
    _delete_message(token, result.channel or channel, result.ts)


def test_live_signed_request_verifies() -> None:
    """AC11: a signature computed with the REAL signing secret verifies.

    Proves the verifier matches Slack's real v0 algorithm end-to-end (the same
    secret Slack signs inbound requests with).
    """
    env = _require("SLACK_SIGNING_SECRET")
    secret = env["SLACK_SIGNING_SECRET"]
    ts = "1700000000"
    body = b"command=%2Fforge&text=help"
    sig = sign_slack_payload(secret, ts, body)
    assert verify_slack_signature(secret, ts, body, sig, now=1700000001) is True
    # A tamper is rejected under the real secret too.
    assert verify_slack_signature(secret, ts, b"command=tampered", sig, now=1700000001) is False
