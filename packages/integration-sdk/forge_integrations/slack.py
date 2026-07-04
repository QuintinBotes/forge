"""Slack notifier (plan Task 1.13): task status + approval requests.

Built against ``httpx`` with an injectable transport (tests use
``httpx.MockTransport`` — no live Slack). Method surface matches the frozen
``forge_contracts.SlackNotifier`` Protocol (plus ``health``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
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
    """A Slack Web API notifier with bounded, rate-limit-aware retries.

    Delivery goes through an injectable ``httpx`` transport (tests use
    ``httpx.MockTransport`` — no live Slack). The method surface matches the
    frozen ``forge_contracts.SlackNotifier`` Protocol (plus ``health`` and
    ``update_message``).

    Retries are bounded and honour Slack's back-pressure signals: a ``429`` waits
    ``Retry-After`` seconds (capped), a ``5xx`` backs off exponentially (capped),
    and a ``200`` body carrying ``{"ok": false, "error": "rate_limited"}`` is
    treated like a ``429``. On terminal failure the methods return
    ``SlackDeliveryResult(ok=False, error=…)`` — they never raise into the caller,
    so an approval-delivery failure degrades gracefully.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        default_channel: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        retry_cap_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._default_channel = default_channel
        self._max_retries = max(0, max_retries)
        self._retry_base_delay = retry_base_delay
        self._retry_cap_seconds = retry_cap_seconds
        self._sleep = sleep
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def __repr__(self) -> str:
        """Secret-safe repr — the bot token is NEVER rendered (AC8)."""
        return (
            f"SlackNotifier(default_channel={self._default_channel!r}, "
            f"configured={self._token is not None})"
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
            resp = self._post("/chat.postMessage", payload)
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

    def update_message(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackDeliveryResult:
        """Edit an already-posted message in place via ``chat.update``.

        Used by the interactivity handler to render "Approved by …" on the
        original approval message. Same bounded, retry-aware path as
        :meth:`notify`; never raises.
        """
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks

        try:
            resp = self._post("/chat.update", payload)
        except httpx.HTTPError as exc:
            return SlackDeliveryResult(ok=False, channel=channel, error=str(exc))

        if resp.status_code >= 400:
            return SlackDeliveryResult(ok=False, channel=channel, error=f"http {resp.status_code}")
        data = resp.json()
        return SlackDeliveryResult(
            ok=bool(data.get("ok")),
            channel=data.get("channel") or channel,
            ts=data.get("ts") or ts,
            error=data.get("error"),
        )

    def health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            resp = self._post("/auth.test", {})
        except httpx.HTTPError as exc:
            return HealthResult(healthy=False, status="error", message=str(exc), checked_at=_now())
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
    # retry / backoff (bounded; honours Retry-After + 5xx backoff)        #
    # ------------------------------------------------------------------ #

    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        """POST ``payload`` to ``path``, retrying transient failures.

        Retries a ``429`` (honouring ``Retry-After``), a ``5xx`` (exponential
        backoff), and a ``200`` body signalling ``rate_limited`` up to
        ``max_retries`` extra attempts. Returns the final :class:`httpx.Response`
        (the caller interprets a terminal 4xx/5xx as ``ok=False``); re-raises
        :class:`httpx.HTTPError` only after the retry budget is exhausted.
        """
        attempt = 0
        while True:
            try:
                resp = self._client.post(path, json=payload)
            except httpx.HTTPError:
                if attempt >= self._max_retries:
                    raise
                self._sleep(self._backoff_delay(attempt))
                attempt += 1
                continue
            delay = self._retry_delay(resp, attempt)
            if delay is None or attempt >= self._max_retries:
                return resp
            self._sleep(delay)
            attempt += 1

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float | None:
        """Return the seconds to wait before a retry, or ``None`` if terminal."""
        if resp.status_code == 429:
            return self._retry_after_delay(resp)
        if resp.status_code >= 500:
            return self._backoff_delay(attempt)
        if resp.status_code < 400:
            try:
                data = resp.json()
            except ValueError:
                return None
            if (
                isinstance(data, dict)
                and data.get("ok") is False
                and data.get("error") == "rate_limited"
            ):
                return self._retry_after_delay(resp)
        return None

    def _retry_after_delay(self, resp: httpx.Response) -> float:
        """Seconds from a ``Retry-After`` header (falling back to the base), capped."""
        header = resp.headers.get("Retry-After")
        try:
            seconds = float(header) if header is not None else self._retry_base_delay
        except (TypeError, ValueError):
            seconds = self._retry_base_delay
        return min(seconds, self._retry_cap_seconds)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff ``base * 2**attempt``, capped at ``retry_cap_seconds``."""
        return min(self._retry_base_delay * (2**attempt), self._retry_cap_seconds)

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
        payload_channel = request.payload.get("channel") or request.payload.get("slack_channel")
        if payload_channel:
            return str(payload_channel)
        return self._default_channel or FALLBACK_CHANNEL

    @staticmethod
    def _button(label: str, action: str, ref: str, *, style: str | None = None) -> dict[str, Any]:
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
