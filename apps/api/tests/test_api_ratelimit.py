"""HARD-11 AC6: rate limiting — headers, per-route overrides, exemptions."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.security.ratelimit import (
    RateLimitMiddleware,
    TokenBucket,
    parse_rate,
)
from forge_api.settings import Settings


def _bare_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/knowledge/search")
    def search() -> dict[str, str]:
        return {"ok": "search"}

    @app.get("/other")
    def other() -> dict[str, str]:
        return {"ok": "other"}

    return app


def test_parse_rate() -> None:
    assert parse_rate("120/minute") == (120, 120)
    assert parse_rate("5/second") == (300, 5)
    assert parse_rate("60/hour") == (1, 60)
    with pytest.raises(ValueError):
        parse_rate("0/minute")


def test_token_bucket_consume_reports_state() -> None:
    bucket = TokenBucket(rate_per_min=60, burst=2)
    r1 = bucket.consume("k", now=0.0)
    assert r1.allowed and r1.remaining == 1 and r1.limit == 2
    r2 = bucket.consume("k", now=0.0)
    assert r2.allowed and r2.remaining == 0
    r3 = bucket.consume("k", now=0.0)
    assert not r3.allowed and r3.remaining == 0 and r3.retry_after_s is not None


def test_ratelimit_headers_on_allowed_and_429() -> None:
    app = _bare_app()
    app.add_middleware(RateLimitMiddleware, rate_per_min=60, burst=2)
    with TestClient(app) as client:
        ok = client.get("/other")
        assert ok.status_code == 200
        assert ok.headers["x-ratelimit-limit"] == "2"
        assert "x-ratelimit-remaining" in ok.headers
        # Exhaust the bucket.
        client.get("/other")
        limited = client.get("/other")
        assert limited.status_code == 429
        assert limited.headers.get("retry-after")
        assert limited.headers["x-ratelimit-remaining"] == "0"


def test_health_never_limited() -> None:
    app = _bare_app()
    app.add_middleware(RateLimitMiddleware, rate_per_min=60, burst=1)
    with TestClient(app) as client:
        # Way over the burst budget, but /health is exempt.
        assert all(client.get("/health").status_code == 200 for _ in range(10))


def test_per_route_override_is_tighter() -> None:
    app = _bare_app()
    app.add_middleware(
        RateLimitMiddleware,
        rate_per_min=1000,
        burst=1000,
        overrides={"/knowledge/search": "2/minute"},
    )
    with TestClient(app) as client:
        assert client.get("/knowledge/search").status_code == 200
        assert client.get("/knowledge/search").status_code == 200
        # The override budget (2) is spent while the default (1000) is intact.
        assert client.get("/knowledge/search").status_code == 429
        assert client.get("/other").status_code == 200


def test_disabled_is_noop() -> None:
    app = _bare_app()
    app.add_middleware(RateLimitMiddleware, rate_per_min=1, burst=1, enabled=False)
    with TestClient(app) as client:
        assert all(client.get("/other").status_code == 200 for _ in range(5))


def test_create_app_honours_disabled_flag() -> None:
    app = create_app(Settings(ratelimit_enabled=False))
    with TestClient(app) as client:
        # Unauthenticated, but rate limiting off: never 429 (auth handles 401).
        codes = [client.get("/board/tasks").status_code for _ in range(10)]
        assert 429 not in codes
