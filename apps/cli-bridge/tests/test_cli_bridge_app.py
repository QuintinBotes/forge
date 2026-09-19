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
    assert "clis" in response.json()


def test_complete_requires_a_token():
    assert _client().post("/v1/complete", json=_body()).status_code == 401


def test_complete_rejects_a_wrong_token():
    response = _client().post(
        "/v1/complete", json=_body(), headers={"Authorization": "Bearer wrong"}
    )
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
    assert "sk-abcdef0123456789" not in caplog.text, "raw stderr secret must never reach the log"


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


def test_main_module_refuses_to_build_without_a_token(monkeypatch):
    import importlib

    monkeypatch.delenv("FORGE_CLI_BRIDGE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        importlib.reload(importlib.import_module("forge_cli_bridge.main"))
