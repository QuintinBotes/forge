"""HARD-11 AC7: request idempotency middleware (offline)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from forge_api.middleware.idempotency import (
    IdempotencyMiddleware,
    InMemoryIdempotencyStore,
)


def _counting_app() -> tuple[FastAPI, dict[str, int]]:
    counter = {"n": 0}
    app = FastAPI()

    @app.post("/work")
    async def work(request: Request) -> dict[str, int]:
        counter["n"] += 1
        body = await request.json()
        return {"run": counter["n"], "echo": body.get("v", 0)}

    @app.get("/read")
    def read() -> dict[str, str]:
        return {"ok": "read"}

    return app, counter


def _client(store: InMemoryIdempotencyStore | None = None) -> tuple[TestClient, dict[str, int]]:
    app, counter = _counting_app()
    app.add_middleware(IdempotencyMiddleware, store=store or InMemoryIdempotencyStore(), ttl_s=60)
    return TestClient(app), counter


def test_same_key_same_body_runs_once_and_replays() -> None:
    client, counter = _client()
    headers = {"Idempotency-Key": "abc"}
    first = client.post("/work", json={"v": 1}, headers=headers)
    second = client.post("/work", json={"v": 1}, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()  # identical response body
    assert counter["n"] == 1  # handler ran exactly once
    assert second.headers.get("idempotency-replayed") == "true"
    assert "idempotency-replayed" not in first.headers


def test_same_key_different_body_is_422() -> None:
    client, counter = _client()
    headers = {"Idempotency-Key": "k2"}
    client.post("/work", json={"v": 1}, headers=headers)
    mismatch = client.post("/work", json={"v": 999}, headers=headers)
    assert mismatch.status_code == 422
    assert counter["n"] == 1  # the mismatched retry never ran


def test_no_key_is_passthrough() -> None:
    client, counter = _client()
    client.post("/work", json={"v": 1})
    client.post("/work", json={"v": 1})
    assert counter["n"] == 2  # no dedup without a key


def test_safe_methods_untouched() -> None:
    client, _ = _client()
    r1 = client.get("/read")
    r2 = client.get("/read")
    assert r1.status_code == r2.status_code == 200
    assert "idempotency-replayed" not in r2.headers


def test_disabled_is_noop() -> None:
    app, counter = _counting_app()
    app.add_middleware(IdempotencyMiddleware, enabled=False)
    client = TestClient(app)
    headers = {"Idempotency-Key": "x"}
    client.post("/work", json={"v": 1}, headers=headers)
    client.post("/work", json={"v": 1}, headers=headers)
    assert counter["n"] == 2


def test_store_put_if_absent_semantics() -> None:
    from datetime import UTC, datetime

    from forge_api.middleware.idempotency import StoredResponse

    store = InMemoryIdempotencyStore()
    value = StoredResponse(
        request_hash="h",
        status_code=200,
        body=b"{}",
        created_at=datetime.now(UTC),
    )
    assert store.put_if_absent("k", value, 60) is True
    assert store.put_if_absent("k", value, 60) is False
    assert store.get("k") is not None
    assert store.get("missing") is None


def test_server_error_not_cached() -> None:
    app = FastAPI()
    calls = {"n": 0}

    @app.post("/boom")
    def boom() -> dict[str, str]:
        calls["n"] += 1
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="down")

    app.add_middleware(IdempotencyMiddleware, ttl_s=60)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Idempotency-Key": "e1"}
    client.post("/boom", headers=headers)
    client.post("/boom", headers=headers)
    # 5xx is not cached, so a retry genuinely re-runs the handler.
    assert calls["n"] == 2
