"""In-process fixed-window rate limiter (F37).

Satisfies the ``forge_contracts.auth.RateLimiter`` protocol. Suitable for a
single-process deployment and for tests; a Redis-backed implementation is a
drop-in behind the same protocol for multi-replica deployments (see slice §12).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from forge_contracts.auth import RateDecision

__all__ = ["InMemoryRateLimiter"]


class InMemoryRateLimiter:
    """Fixed-window counter per key: at most ``limit`` hits per ``window_seconds``."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    async def check(self, key: str, *, limit: int, window_seconds: int) -> RateDecision:
        """Record one hit against ``key`` and decide whether it is allowed."""
        now = self._clock()
        window_start, count = self._windows.get(key, (now, 0))
        if now - window_start >= window_seconds:
            window_start, count = now, 0
        if count >= limit:
            retry_after = max(0.0, window_seconds - (now - window_start))
            return RateDecision(allowed=False, remaining=0, retry_after_seconds=retry_after)
        self._windows[key] = (window_start, count + 1)
        return RateDecision(allowed=True, remaining=limit - count - 1)
