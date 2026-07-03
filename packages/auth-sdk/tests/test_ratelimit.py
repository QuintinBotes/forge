"""F37 in-process rate limiter tests (AC18 core semantics)."""

from __future__ import annotations

from forge_auth.ratelimit import InMemoryRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_allows_under_limit_then_blocks() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    decisions = [await limiter.check("u:1", limit=3, window_seconds=60) for _ in range(3)]
    assert all(d.allowed for d in decisions)
    assert [d.remaining for d in decisions] == [2, 1, 0]

    blocked = await limiter.check("u:1", limit=3, window_seconds=60)
    assert not blocked.allowed
    assert blocked.retry_after_seconds > 0


async def test_window_reset_allows_again() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock=clock)
    for _ in range(3):
        await limiter.check("u:1", limit=3, window_seconds=60)
    assert not (await limiter.check("u:1", limit=3, window_seconds=60)).allowed
    clock.now = 61.0
    assert (await limiter.check("u:1", limit=3, window_seconds=60)).allowed


async def test_buckets_are_independent() -> None:
    limiter = InMemoryRateLimiter(clock=FakeClock())
    for _ in range(2):
        await limiter.check("ws:1", limit=2, window_seconds=60)
    assert not (await limiter.check("ws:1", limit=2, window_seconds=60)).allowed
    assert (await limiter.check("user:1", limit=2, window_seconds=60)).allowed
