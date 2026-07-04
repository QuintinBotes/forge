"""HARD-01 unit tests: retry / backoff / rate-limit handling (offline).

A fake ``sleep`` records every requested delay so backoff is asserted without
any real waiting. A fake ``token_provider`` counts mints and reauth cycles.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import make_transport

from forge_integrations import GitHubClient, GitHubError, RetryPolicy
from forge_integrations.github import _NO_RETRY


class _RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _app_client(
    handler,
    *,
    retry: RetryPolicy,
    sleep,
    tokens: list[str] | None = None,
    invalidations: list[int] | None = None,
    wall_clock=None,
) -> GitHubClient:
    """A client wired like ``from_app`` but with an injected token provider."""
    seq = tokens or ["tok-a"]
    state = {"i": 0}

    def token_provider() -> str:
        return seq[min(state["i"], len(seq) - 1)]

    def invalidate() -> None:
        state["i"] += 1
        if invalidations is not None:
            invalidations.append(1)

    return GitHubClient(
        transport=make_transport(handler),
        retry=retry,
        token_provider=token_provider,
        invalidate=invalidate,
        sleep=sleep,
        wall_clock=wall_clock,
        rng=lambda: 0.5,  # deterministic jitter factor -> *1.0
    )


def test_retries_on_5xx_with_backoff() -> None:
    sleep = _RecordingSleep()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={"ok": True})

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=4, base_delay_s=0.5, jitter=False),
        sleep=sleep,
    )
    resp = client._request("GET", "/rate_limit", action="health")
    assert resp.status_code == 200
    assert len(attempts) == 3
    # Two backoff sleeps: 0.5 * 2^0, 0.5 * 2^1.
    assert sleep.delays == [0.5, 1.0]


def test_gives_up_after_max_attempts_on_5xx() -> None:
    sleep = _RecordingSleep()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, json={"message": "boom"})

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=3, base_delay_s=0.1, jitter=False),
        sleep=sleep,
    )
    resp = client._request("GET", "/x", action="op")
    # Terminal 500 returned to the caller after exhausting attempts.
    assert resp.status_code == 500
    assert len(attempts) == 3
    assert len(sleep.delays) == 2


def test_retry_once_on_401_after_invalidate() -> None:
    sleep = _RecordingSleep()
    invalidations: list[int] = []
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        attempts.append(auth)
        if len(attempts) == 1:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"ok": True})

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=4, jitter=False),
        sleep=sleep,
        tokens=["stale", "fresh"],
        invalidations=invalidations,
    )
    resp = client._request("GET", "/rate_limit", action="health")
    assert resp.status_code == 200
    assert len(invalidations) == 1
    # First call used the stale token, the retry used the freshly-minted one.
    assert attempts[0] == "Bearer stale"
    assert attempts[1] == "Bearer fresh"


def test_persistent_401_not_looped_forever() -> None:
    sleep = _RecordingSleep()
    invalidations: list[int] = []
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=4, jitter=False),
        sleep=sleep,
        tokens=["a", "b"],
        invalidations=invalidations,
    )
    resp = client._request("GET", "/x", action="op")
    assert resp.status_code == 401
    # Exactly one reauth attempt (2 total requests); no infinite loop.
    assert len(invalidations) == 1
    assert len(attempts) == 2


def test_respects_retry_after_header() -> None:
    sleep = _RecordingSleep()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(
                403,
                headers={"Retry-After": "7"},
                json={"message": "secondary rate limit"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=3, jitter=False),
        sleep=sleep,
    )
    resp = client._request("GET", "/x", action="op")
    assert resp.status_code == 200
    # Slept exactly the Retry-After duration (no real sleep, fake clock).
    assert sleep.delays == [7.0]


def test_respects_ratelimit_reset_when_remaining_zero() -> None:
    sleep = _RecordingSleep()
    attempts: list[int] = []
    now = 1_000_000.0

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(
                429,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(now + 12)),
                },
                json={"message": "rate limited"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=3, jitter=False),
        sleep=sleep,
        wall_clock=lambda: now,
    )
    resp = client._request("GET", "/x", action="op")
    assert resp.status_code == 200
    # Waited until the reset epoch (12s out).
    assert sleep.delays == [12.0]


def test_404_raises_immediately() -> None:
    sleep = _RecordingSleep()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, json={"message": "not found"})

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=4, jitter=False),
        sleep=sleep,
    )
    resp = client._request("GET", "/missing", action="op")
    # 404 is not retried; a single request, no sleeps.
    assert resp.status_code == 404
    assert len(attempts) == 1
    assert sleep.delays == []
    # And the public surface surfaces it as a GitHubError(404).
    with pytest.raises(GitHubError) as exc:
        client._raise_for_status(resp)
    assert exc.value.status_code == 404


def test_plain_403_permission_denied_not_retried() -> None:
    sleep = _RecordingSleep()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        # No Retry-After and remaining != 0 -> a genuine permission error.
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "42"},
            json={"message": "Resource not accessible by integration"},
        )

    client = _app_client(
        handler,
        retry=RetryPolicy(max_attempts=4, jitter=False),
        sleep=sleep,
    )
    resp = client._request("GET", "/x", action="op")
    assert resp.status_code == 403
    assert len(attempts) == 1
    assert sleep.delays == []


def test_legacy_static_client_does_not_retry() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, json={"message": "unavailable"})

    client = GitHubClient(token="ghs_x", transport=make_transport(handler))
    assert client._retry is _NO_RETRY
    resp = client._request("GET", "/x")
    assert resp.status_code == 503
    assert len(attempts) == 1  # single attempt, no retry loop
