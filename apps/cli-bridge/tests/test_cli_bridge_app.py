import pytest
from fastapi.testclient import TestClient

from forge_cli_bridge.app import create_app
from forge_cli_bridge.settings import BridgeSettings

ENVELOPE = '{"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}'


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


def test_complete_returns_a_model_response(monkeypatch):
    import subprocess

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=ENVELOPE, stderr="")

    app_client = _client()
    app_client.app.state.runner = runner
    response = app_client.post(
        "/v1/complete", json=_body(), headers={"Authorization": "Bearer t0ken"}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "ok"


def test_main_module_refuses_to_build_without_a_token(monkeypatch):
    import importlib

    monkeypatch.delenv("FORGE_CLI_BRIDGE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        importlib.reload(importlib.import_module("forge_cli_bridge.main"))
