"""HARD-11 AC5: graceful shutdown + drain-aware readiness (offline)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.middleware import shutdown as shutdown_mod
from forge_api.middleware.shutdown import (
    ShutdownState,
    current_state,
    lifespan,
    set_current_state,
)
from forge_api.settings import Settings


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    set_current_state(ShutdownState(serving=True))
    yield
    set_current_state(None)


def test_liveness_always_ok() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/healthz").status_code == 200


def test_readiness_ok_while_serving() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        assert resp.json()["checks"]["serving"] == "ok"


def test_readiness_503_once_draining() -> None:
    with TestClient(create_app()) as client:
        # Simulate SIGTERM: begin draining -> readiness must flip to 503.
        current_state().begin_drain()
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "draining"
        # Liveness still 200 (the process is up, just draining).
        assert client.get("/health").status_code == 200


def test_readiness_requires_deps_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the hard-gate on and the DB unreachable, readiness is 503.
    import forge_api.routers.health as health

    def _boom(checks: dict[str, str]) -> bool:
        checks["database"] = "error: Boom"
        return False

    monkeypatch.setattr(health, "_probe_dependencies", _boom)
    app = create_app(Settings(readiness_require_deps=True))
    with TestClient(app) as client:
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["checks"]["database"].startswith("error")


async def test_lifespan_disposes_engine_and_closes_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = {"engine": False, "redis": False, "flushed": False}

    monkeypatch.setattr(
        shutdown_mod, "dispose_db_engine", lambda: disposed.__setitem__("engine", True)
    )
    monkeypatch.setattr(
        shutdown_mod,
        "shutdown_close_redis",
        lambda client: disposed.__setitem__("redis", True),
    )
    monkeypatch.setattr(
        shutdown_mod, "flush_exporters", lambda: disposed.__setitem__("flushed", True)
    )

    app = create_app()
    async with lifespan(app):
        assert current_state().is_serving is True
        state = app.state.shutdown_state
    # After the lifespan exits (shutdown), draining began and resources released.
    assert state.is_serving is False
    assert disposed == {"engine": True, "redis": True, "flushed": True}


def test_dispose_db_engine_noop_when_never_built(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the engine cache is empty, dispose must not build one just to close it.
    from forge_api import db

    monkeypatch.setattr(db.get_engine, "cache_clear", db.get_engine.cache_clear)
    db.get_engine.cache_clear()
    # Should not raise even though no engine exists yet.
    shutdown_mod.dispose_db_engine()


def test_shutdown_close_redis_handles_none_and_client() -> None:
    shutdown_mod.shutdown_close_redis(None)  # no-op

    class _R:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    r = _R()
    shutdown_mod.shutdown_close_redis(r)
    assert r.closed is True
