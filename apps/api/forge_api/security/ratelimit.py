"""Per-caller request rate limiting (HARD-09, extended by HARD-11).

A pure in-process token bucket keyed by the presented API credential (which
maps 1:1 to a principal) falling back to the client IP for anonymous callers.
Exceeding the budget returns ``429 Too Many Requests`` with a ``Retry-After``
header and the ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining`` /
``X-RateLimit-Reset`` triad (also emitted on allowed responses so a well-behaved
client can self-throttle). ``/health`` (and ``/``) stay exempt so liveness
probes are unaffected.

HARD-11 adds **per-route overrides** so the expensive hot paths
(``/knowledge/search``, ``/knowledge/retrieve``, agent-run enqueue, ``/index``,
``/sync``) can carry a tighter budget than the default, and surfaces the standard
rate-limit response headers.

The limiter is deliberately per-process: in a multi-replica deployment the
effective limit scales with replica count (documented in
``docs/self-hosting/security.md`` and ``docs/self-hosting/reliability.md``; a
shared Redis-backed limiter is future work). Keying happens at the ASGI layer
*before* routing, so the credential string is hashed — the raw key value never
lands in the bucket map.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "BucketResult",
    "RateLimitMiddleware",
    "TokenBucket",
    "parse_rate",
]

_WINDOW_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600}


def parse_rate(spec: str) -> tuple[int, int]:
    """Parse a ``"N/window"`` budget into ``(rate_per_min, burst)``.

    ``"120/minute"`` → ``(120, 120)``; ``"5/second"`` → ``(300, 5)``. The burst
    is the window budget ``N`` (a client may spend the whole window at once), and
    ``rate_per_min`` is the equivalent steady refill rate.
    """
    count_str, _, window = spec.strip().partition("/")
    count = int(count_str)
    if count <= 0:
        raise ValueError("rate count must be positive")
    window_s = _WINDOW_SECONDS.get(window.strip().lower(), 60)
    rate_per_min = max(1, round(count * 60 / window_s))
    return rate_per_min, count


@dataclass(frozen=True)
class BucketResult:
    """Outcome of consuming one token."""

    allowed: bool
    remaining: int
    limit: int
    reset_s: int
    retry_after_s: int | None


class TokenBucket:
    """Classic token bucket: ``burst`` capacity refilled at ``rate_per_min``."""

    def __init__(self, *, rate_per_min: int, burst: int) -> None:
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self._rate_per_sec = rate_per_min / 60.0
        self._burst = float(burst)
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, stamp)
        self._lock = threading.Lock()

    @property
    def retry_after_seconds(self) -> int:
        """Seconds until at least one token is available again (ceiling)."""
        return max(1, int(1.0 / self._rate_per_sec + 0.999))

    def consume(self, key: str, *, now: float | None = None) -> BucketResult:
        """Consume one token for ``key`` and report the limit state."""
        stamp = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self._burst, stamp))
            tokens = min(self._burst, tokens + (stamp - last) * self._rate_per_sec)
            limit = int(self._burst)
            if tokens < 1.0:
                self._buckets[key] = (tokens, stamp)
                reset = max(1, math.ceil((1.0 - tokens) / self._rate_per_sec))
                return BucketResult(False, 0, limit, reset, self.retry_after_seconds)
            tokens -= 1.0
            self._buckets[key] = (tokens, stamp)
            remaining = int(tokens)
            reset = max(0, math.ceil((self._burst - tokens) / self._rate_per_sec))
            return BucketResult(True, remaining, limit, reset, None)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token for ``key``; False when the bucket is empty."""
        return self.consume(key, now=now).allowed


def _caller_key(scope: Scope) -> str:
    """Stable limiter key: hash of the credential header, else the client IP."""
    credential: bytes | None = None
    for name, value in scope.get("headers", []):
        if name in (b"authorization", b"x-api-key"):
            credential = value
            break
    if credential:
        return "cred:" + hashlib.sha256(credential).hexdigest()[:32]
    client = scope.get("client")
    return f"ip:{client[0]}" if client else "ip:unknown"


def _ratelimit_headers(result: BucketResult) -> list[tuple[bytes, bytes]]:
    return [
        (b"x-ratelimit-limit", str(result.limit).encode("ascii")),
        (b"x-ratelimit-remaining", str(result.remaining).encode("ascii")),
        (b"x-ratelimit-reset", str(result.reset_s).encode("ascii")),
    ]


_DEFAULT_EXEMPT = frozenset({"/health", "/healthz", "/health/ready", "/readyz", "/"})


class RateLimitMiddleware:
    """ASGI middleware returning 429 + rate-limit headers over the budget."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_per_min: int = 120,
        burst: int = 60,
        enabled: bool = True,
        exempt_paths: frozenset[str] = _DEFAULT_EXEMPT,
        overrides: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self.enabled = enabled
        self.exempt_paths = exempt_paths
        self._bucket = TokenBucket(rate_per_min=rate_per_min, burst=burst)
        # Per-route override buckets, longest-prefix-wins.
        self._overrides: list[tuple[str, TokenBucket]] = []
        for route, spec in (overrides or {}).items():
            rpm, b = parse_rate(spec)
            self._overrides.append((route, TokenBucket(rate_per_min=rpm, burst=b)))
        self._overrides.sort(key=lambda item: len(item[0]), reverse=True)

    def _bucket_for(self, path: str) -> TokenBucket:
        for route, bucket in self._overrides:
            if path == route or path.startswith(route.rstrip("/") + "/"):
                return bucket
        return self._bucket

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self.enabled
            or scope["type"] != "http"
            or scope.get("path", "") in self.exempt_paths
        ):
            await self.app(scope, receive, send)
            return

        result = self._bucket_for(scope.get("path", "")).consume(_caller_key(scope))
        if not result.allowed:
            body = b'{"detail":"Too Many Requests"}'
            headers: list[tuple[bytes, bytes]] = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", str(result.retry_after_s or 1).encode("ascii")),
                *_ratelimit_headers(result),
            ]
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        # Allowed: inject the rate-limit headers on the response start message.
        extra = _ratelimit_headers(result)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)

    # Test seam: expose the bucket so suites can assert boundary math directly.
    @property
    def bucket(self) -> TokenBucket:
        return self._bucket
