"""Per-caller request rate limiting (HARD-09).

A pure in-process token bucket keyed by the presented API credential (which
maps 1:1 to a principal) falling back to the client IP for anonymous callers.
Exceeding the budget returns ``429 Too Many Requests`` with a ``Retry-After``
header. ``/health`` (and ``/``) stay exempt so liveness probes are unaffected.

The limiter is deliberately per-process: in a multi-replica deployment the
effective limit scales with replica count (documented in
``docs/self-hosting/security.md``; a shared Redis-backed limiter is future
work). Keying happens at the ASGI layer *before* routing, so the credential
string is hashed — the raw key value never lands in the bucket map.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["RateLimitMiddleware", "TokenBucket"]


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

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token for ``key``; False when the bucket is empty."""
        stamp = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self._burst, stamp))
            tokens = min(self._burst, tokens + (stamp - last) * self._rate_per_sec)
            if tokens < 1.0:
                self._buckets[key] = (tokens, stamp)
                return False
            self._buckets[key] = (tokens - 1.0, stamp)
            return True


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


class RateLimitMiddleware:
    """ASGI middleware returning 429 + ``Retry-After`` over the request budget."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        rate_per_min: int = 120,
        burst: int = 60,
        enabled: bool = True,
        exempt_paths: frozenset[str] = frozenset({"/health", "/"}),
    ) -> None:
        self.app = app
        self.enabled = enabled
        self.exempt_paths = exempt_paths
        self._bucket = TokenBucket(rate_per_min=rate_per_min, burst=burst)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self.enabled
            or scope["type"] != "http"
            or scope.get("path", "") in self.exempt_paths
        ):
            await self.app(scope, receive, send)
            return
        if self._bucket.allow(_caller_key(scope)):
            await self.app(scope, receive, send)
            return
        retry_after = str(self._bucket.retry_after_seconds)
        body = b'{"detail":"Too Many Requests"}'
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"retry-after", retry_after.encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    # Test seam: expose the bucket so suites can assert boundary math directly.
    @property
    def bucket(self) -> TokenBucket:
        return self._bucket
