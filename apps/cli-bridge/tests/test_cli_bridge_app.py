import asyncio
import logging
import subprocess
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from forge_cli_bridge.app import create_app
from forge_cli_bridge.settings import BridgeSettings

ENVELOPE = '{"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}'


def _which_only_claude(name: str) -> str | None:
    """A fake `which` that reports exactly one CLI present, regardless of what
    is actually on this machine's PATH. Fix round 1, IMPORTANT: the suite must
    be hermetic -- CI installs neither claude nor codex, so a test that relies
    on the real detect_clis()/shutil.which would silently short-circuit to
    provider_unavailable there and fail. Injected via app.state.which, the
    same seam detect_clis() itself exposes.
    """
    return "/usr/local/bin/claude" if name == "claude" else None


def _client(**overrides):
    settings = BridgeSettings(token="t0ken", **overrides)
    return TestClient(create_app(settings))


def _body():
    return {"model": "claude", "messages": [{"role": "user", "content": "hi"}]}


def test_health_reports_detected_clis():
    response = _client().get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "clis" in body
    assert body["non_loopback_override_set"] is False


def test_health_reports_non_loopback_override_when_set():
    """Fix round 2, IMPORTANT: /health no longer claims a "loopback_only"
    property it cannot verify (main.py's bind can bypass BridgeSettings
    entirely via `uvicorn forge_cli_bridge.main:app --host 0.0.0.0`). It now
    reports only the one thing genuinely known in-process: whether this
    settings object's override was set.
    """
    settings = BridgeSettings(token="t0ken", host="0.0.0.0", allow_non_loopback=True)
    response = TestClient(create_app(settings)).get("/health")
    assert response.json()["non_loopback_override_set"] is True


def test_complete_requires_a_token():
    assert _client().post("/v1/complete", json=_body()).status_code == 401


def test_complete_rejects_a_wrong_token():
    response = _client().post(
        "/v1/complete", json=_body(), headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


async def test_complete_rejects_a_non_ascii_token_with_401_not_500():
    """Fix round 2, regression (introduced by round 1's own hmac fix):
    hmac.compare_digest raises TypeError on a non-ASCII str, and Starlette
    decodes header values as latin-1, so "Authorization: Bearer t\\xf6ken"
    used to produce an unhandled TypeError -> HTTP 500 with a traceback in
    the log, where the old plain `!=` returned a clean 401.

    A plain httpx str header can't reproduce this (httpx itself refuses to
    ascii-encode a non-ASCII str client-side, before it would ever reach the
    app); building the request with a raw (bytes, bytes) header pair and
    sending it over ASGITransport reproduces exactly what Starlette hands
    the route: a latin-1-decoded str containing a non-ASCII character.
    """
    app = create_app(BridgeSettings(token="t0ken"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge.test") as client:
        request = httpx.Request(
            "POST",
            "http://bridge.test/v1/complete",
            headers=[(b"authorization", b"Bearer t\xf6ken")],
            json=_body(),
        )
        response = await client.send(request)
    assert response.status_code == 401


def test_settings_refuse_an_empty_token():
    with pytest.raises(ValueError):
        BridgeSettings(token="")


def test_settings_refuse_non_loopback_without_override():
    with pytest.raises(ValueError):
        BridgeSettings(token="t0ken", host="0.0.0.0")


def test_settings_allow_non_loopback_with_override():
    settings = BridgeSettings(token="t0ken", host="0.0.0.0", allow_non_loopback=True)
    assert settings.host == "0.0.0.0"


def test_settings_is_loopback_reflects_host():
    assert BridgeSettings(token="t0ken").is_loopback is True
    assert BridgeSettings(token="t0ken", host="0.0.0.0", allow_non_loopback=True).is_loopback is (
        False
    )


def test_settings_refuse_a_non_positive_concurrency_limit():
    with pytest.raises(ValueError):
        BridgeSettings(token="t0ken", max_concurrent_completions=0)


def test_complete_returns_a_model_response(monkeypatch):
    """Fix round 1, IMPORTANT: hermetic regardless of the host's real PATH --
    an empty PATH proves detect_clis()'s result comes from the injected
    `which`, not from claude/codex actually being installed here.
    """
    monkeypatch.setenv("PATH", "")

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=ENVELOPE, stderr="")

    app_client = _client()
    app_client.app.state.runner = runner
    app_client.app.state.which = _which_only_claude
    response = app_client.post(
        "/v1/complete", json=_body(), headers={"Authorization": "Bearer t0ken"}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "ok"


def test_complete_redacts_secrets_from_failure_logs(caplog):
    """Fix round 1, minor: the bridge must pass a real redactor into
    LocalCliModelClient, not the identity default -- otherwise raw CLI stderr
    (which can contain secrets) reaches the host log unscrubbed.
    """

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="key sk-abcdef0123456789")

    app_client = _client()
    app_client.app.state.runner = runner
    app_client.app.state.which = _which_only_claude
    with caplog.at_level(logging.WARNING, logger="forge_agent.providers.local_cli"):
        response = app_client.post(
            "/v1/complete", json=_body(), headers={"Authorization": "Bearer t0ken"}
        )
    assert response.status_code == 200
    # Cheap addition from the reviewer's out-of-scope list: this test would
    # pass even if the failure were never logged at all, unless we also
    # assert the (redacted) line actually appears.
    assert "local cli" in caplog.text, "expected the CLI-failure warning to be logged"
    assert "[REDACTED]" in caplog.text, "expected the redactor's replacement token in the log"
    assert "sk-abcdef0123456789" not in caplog.text, "raw stderr secret must never reach the log"


def test_default_redactor_warns_when_forge_api_is_unavailable(monkeypatch, caplog):
    """Fix round 2, minor: a standalone self-host install (the deployment
    this bridge targets) will not have forge_api installed -- that is the
    COMMON case for an external user, not an edge case. Degrading to
    identity silently would mean "no redaction, no warning, raw CLI stderr
    in the host log" by default; the downgrade must be visible.
    """
    import sys

    from forge_cli_bridge.app import _default_redactor

    monkeypatch.setitem(sys.modules, "forge_api.observability.redaction", None)
    with caplog.at_level(logging.WARNING, logger="forge_cli_bridge"):
        redactor = _default_redactor()

    assert redactor("key sk-abcdef0123456789") == "key sk-abcdef0123456789", (
        "the fallback redactor must be identity, not raise or mangle input"
    )
    assert "UNREDACTED" in caplog.text


def test_startup_banner_is_logged_unconditionally(caplog):
    """Fix round 1, spec gap: the constraint is "README AND startup banner" --
    a banner that only fires when allow_non_loopback is set is not that. The
    default-settings client below never sets allow_non_loopback, so this only
    passes if the banner is unconditional.
    """
    with caplog.at_level(logging.WARNING, logger="forge_cli_bridge"):
        _client()
    assert "DEVELOPMENT / SELF-HOST CONVENIENCE ONLY" in caplog.text
    assert "NOT for a shared or production deployment" in caplog.text


async def test_completion_concurrency_is_bounded():
    """Fix round 1, minor: max_concurrent_completions=1 must serialize CLI
    subprocess spawns -- each call spends real subscription quota and can run
    for up to timeout_s, so an unbounded burst is a plausible accident.

    The fake runner blocks (via a threading.Event, safe since it executes in
    asyncio.to_thread's worker thread, never the event loop) until the test
    has already asserted the high-water mark, so this is deterministic rather
    than a wall-clock race.
    """
    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0
    proceed = threading.Event()

    def runner(argv, **kwargs):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        assert proceed.wait(timeout=5), "test runner was never released"
        with lock:
            in_flight -= 1
        return subprocess.CompletedProcess(argv, 0, stdout=ENVELOPE, stderr="")

    settings = BridgeSettings(token="t0ken", max_concurrent_completions=1)
    app = create_app(settings)
    app.state.runner = runner
    app.state.which = _which_only_claude

    headers = {"Authorization": "Bearer t0ken"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge.test") as client:
        task1 = asyncio.ensure_future(client.post("/v1/complete", json=_body(), headers=headers))
        task2 = asyncio.ensure_future(client.post("/v1/complete", json=_body(), headers=headers))
        await asyncio.sleep(0.2)
        assert max_in_flight == 1, "max_concurrent_completions=1 must serialize CLI spawns"
        proceed.set()
        response1, response2 = await asyncio.gather(task1, task2)

    assert response1.status_code == 200
    assert response2.status_code == 200


def test_bind_guard_allows_loopback(monkeypatch):
    monkeypatch.setenv("FORGE_CLI_BRIDGE_TOKEN", "t0ken")
    from forge_cli_bridge.main import _guard_non_loopback_bind

    _guard_non_loopback_bind(is_loopback=True, allow_non_loopback=False)  # must not raise


def test_bind_guard_allows_explicit_override(monkeypatch):
    monkeypatch.setenv("FORGE_CLI_BRIDGE_TOKEN", "t0ken")
    from forge_cli_bridge.main import _guard_non_loopback_bind

    _guard_non_loopback_bind(is_loopback=False, allow_non_loopback=True)  # must not raise


def test_bind_guard_raises_for_non_loopback_without_override(monkeypatch):
    """Fix round 2, IMPORTANT: the old guard was a bare `assert`, which
    `python -O` strips entirely. It is now an explicit `if: raise
    RuntimeError`, which cannot be compiled away. There is no way to re-run
    the interpreter under `-O` from inside pytest, so this calls the guard
    function directly instead -- an `assert` in *this test* would prove
    nothing about `-O`, but a plain `if not raised: fail` inside
    pytest.raises does not depend on assertions being enabled at all.
    """
    monkeypatch.setenv("FORGE_CLI_BRIDGE_TOKEN", "t0ken")
    from forge_cli_bridge.main import _guard_non_loopback_bind

    with pytest.raises(RuntimeError):
        _guard_non_loopback_bind(is_loopback=False, allow_non_loopback=False)


def test_main_module_refuses_to_build_without_a_token(monkeypatch):
    import importlib

    monkeypatch.delenv("FORGE_CLI_BRIDGE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        importlib.reload(importlib.import_module("forge_cli_bridge.main"))
