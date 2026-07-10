"""Per-connection rate limiting for MCP tool calls (F40 delta 4).

A :class:`RateLimiter` is consulted by :class:`~forge_mcp.client.MCPGatewayClient`
before any live tool call. Exceeding the per-connection budget raises the typed,
retryable :class:`~forge_mcp.exceptions.MCPRateLimitedError` — never a run/tool
failure — so a caller can back off and retry.

Two implementations share one classic token-bucket algorithm (``capacity``
tokens, refilled at ``refill_per_sec``):

* :class:`InMemoryRateLimiter` — a single-process, deterministic bucket used in
  dev/tests (and a safe default when no Redis is configured);
* :class:`RedisTokenBucket` — a distributed bucket whose per-connection state
  lives in Redis, applied **atomically** via a Lua script so concurrent gateway
  replicas share one budget. It degrades open (allows) if Redis is unreachable so
  a transient Redis outage never hard-fails MCP traffic.

``redis_rate_limiter`` builds a :class:`RedisTokenBucket` from a URL, returning
``None`` when the ``redis`` package or a live server is unavailable — the caller
then falls back to no limiter (or the in-memory one).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

__all__ = [
    "InMemoryRateLimiter",
    "RateLimiter",
    "RedisTokenBucket",
    "redis_rate_limiter",
]


@runtime_checkable
class RateLimiter(Protocol):
    """Minimal surface the client needs to admit or reject a call for ``key``."""

    def allow(self, key: str) -> bool: ...

    def retry_after_s(self) -> float: ...


class InMemoryRateLimiter:
    """Single-process token bucket keyed by connection id (deterministic)."""

    def __init__(
        self,
        *,
        capacity: int,
        refill_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be positive")
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, stamp)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True

    def retry_after_s(self) -> float:
        return 1.0 / self._refill


# Atomic token-bucket refill+consume. KEYS[1] is the per-connection state key;
# ARGV = capacity, refill_per_sec, now (epoch seconds). Returns 1 (allowed) / 0.
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', key, 'tokens', 'stamp')
local tokens = tonumber(state[1])
local stamp = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  stamp = now
end
local delta = math.max(0, now - stamp)
tokens = math.min(capacity, tokens + delta * refill)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'stamp', now)
-- Expire idle buckets: time to fully refill from empty, plus a margin.
local ttl = math.ceil(capacity / refill) + 1
redis.call('EXPIRE', key, ttl)
return allowed
"""


class RedisTokenBucket:
    """Distributed per-connection token bucket backed by Redis (atomic Lua)."""

    def __init__(
        self,
        redis_client: Any,
        *,
        capacity: int,
        refill_per_sec: float,
        namespace: str = "mcp:ratelimit",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be positive")
        self._redis = redis_client
        self._capacity = capacity
        self._refill = float(refill_per_sec)
        self._namespace = namespace
        self._clock = clock
        self._script = redis_client.register_script(_LUA_TOKEN_BUCKET)

    def allow(self, key: str) -> bool:
        redis_key = f"{self._namespace}:{key}"
        try:
            result = self._script(
                keys=[redis_key],
                args=[self._capacity, self._refill, self._clock()],
            )
        except Exception:
            # Degrade open: a Redis blip must never hard-fail MCP traffic. The
            # in-process bucket (if wired) still caps a single replica.
            return True
        return bool(int(result))

    def retry_after_s(self) -> float:
        return 1.0 / self._refill


def redis_rate_limiter(
    url: str,
    *,
    capacity: int,
    refill_per_sec: float,
    namespace: str = "mcp:ratelimit",
) -> RedisTokenBucket | None:
    """Build a :class:`RedisTokenBucket` from ``url``, or ``None`` if unavailable.

    Returns ``None`` (so the caller can fall back cleanly) when the ``redis``
    package is not installed or no server answers a ``PING`` — mirroring the
    repo convention of PARKing an external-infra dependency instead of faking it.
    """
    try:
        import redis  # optional dependency, imported lazily
    except ImportError:
        return None
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=1)
        client.ping()
    except Exception:
        return None
    return RedisTokenBucket(
        client, capacity=capacity, refill_per_sec=refill_per_sec, namespace=namespace
    )
