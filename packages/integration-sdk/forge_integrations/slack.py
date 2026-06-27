"""Slack notifier (plan Task 1.13): task status + approval requests.

Built against ``httpx`` with an injectable transport (tests use
``httpx.MockTransport`` — no live Slack). Method surface matches the frozen
``forge_contracts.SlackNotifier`` Protocol (plus ``health``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import httpx

from forge_contracts import (
    ApprovalRequest,
    HealthResult,
    SlackDeliveryResult,
    SlackMessage,
)

DEFAULT_BASE_URL = "https://slack.com/api"
FALLBACK_CHANNEL = "#approvals"


class SlackNotifier:
    """A fixture-backed Slack Web API notifier."""

    def __init__(
        self,
        *,
        token: str | None = None,
        default_channel: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._default_channel = default_channel
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SlackNotifier:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # contract surface                                                   #
    # ------------------------------------------------------------------ #

    def notify(self, message: SlackMessage) -> SlackDeliveryResult:
        payload: dict[str, Any] = {"channel": message.channel, "text": message.text}
        if message.blocks is not None:
            payload["blocks"] = message.blocks
        if message.thread_ts is not None:
            payload["thread_ts"] = message.thread_ts

        try:
            resp = self._client.post("/chat.postMessage", json=payload)
        except httpx.HTTPError as exc:
            return SlackDeliveryResult(ok=False, channel=message.channel, error=str(exc))

        if resp.status_code >= 400:
            return SlackDeliveryResult(
                ok=False, channel=message.channel, error=f"http {resp.status_code}"
            )
        data = resp.json()
        return SlackDeliveryResult(
            ok=bool(data.get("ok")),
            channel=data.get("channel") or message.channel,
            ts=data.get("ts"),
            error=data.get("error"),
        )

    def notify_approval(self, request: ApprovalRequest) -> SlackDeliveryResult:
        return self.notify(self.build_approval_message(request))

    def health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            resp = self._client.post("/auth.test", json={})
        except httpx.HTTPError as exc:
            return HealthResult(
                healthy=False, status="error", message=str(exc), checked_at=_now()
            )
        latency_ms = int((time.perf_counter() - start) * 1000)
        ok = resp.status_code < 400 and bool(resp.json().get("ok"))
        return HealthResult(
            healthy=ok,
            status="ok" if ok else "error",
            latency_ms=latency_ms,
            message=None if ok else "auth.test failed",
            checked_at=_now(),
        )

    # ------------------------------------------------------------------ #
    # formatting                                                         #
    # ------------------------------------------------------------------ #

    def build_approval_message(
        self, request: ApprovalRequest, channel: str | None = None
    ) -> SlackMessage:
        """Format an approval request per the spec's "Approval UI Must Show"."""
        resolved = self._resolve_channel(request, channel)
        gate = request.gate.value
        title = request.title or f"{gate} approval requested"

        lines = [f"*Approval needed: {gate}*", title]
        if request.summary:
            lines.append(request.summary)
        if request.changed_files:
            lines.append(f"Changed files: {len(request.changed_files)}")
        if request.verification:
            passed = sum(1 for c in request.verification if c.passed)
            lines.append(f"Verification: {passed}/{len(request.verification)} checks passed")
        if request.confidence is not None:
            lines.append(f"Confidence: {request.confidence:.2f}")
        if request.risks:
            lines.append("Risks: " + ", ".join(request.risks))
        text = "\n".join(lines)

        ref = str(request.id) if request.id is not None else gate
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "block_id": f"approval:{ref}",
                "elements": [
                    self._button("Approve", "approve", ref, style="primary"),
                    self._button("Reject", "reject", ref, style="danger"),
                    self._button("Request changes", "request_changes", ref),
                ],
            },
        ]
        return SlackMessage(channel=resolved, text=text, blocks=blocks)

    def _resolve_channel(self, request: ApprovalRequest, channel: str | None) -> str:
        if channel:
            return channel
        payload_channel = request.payload.get("channel") or request.payload.get(
            "slack_channel"
        )
        if payload_channel:
            return str(payload_channel)
        return self._default_channel or FALLBACK_CHANNEL

    @staticmethod
    def _button(
        label: str, action: str, ref: str, *, style: str | None = None
    ) -> dict[str, Any]:
        element: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": f"approval_{action}",
            "value": f"{action}:{ref}",
        }
        if style:
            element["style"] = style
        return element


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["SlackNotifier"]
