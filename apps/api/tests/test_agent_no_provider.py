"""An agent run must refuse rather than fake success (#112).

`POST /agent/runs` used to fall back to the offline `ScriptedModelClient`, whose
every objective "finishes cleanly". A deployment with no model provider therefore
returned `status: succeeded` with `confidence: 0.9` having called nothing and
changed nothing — indistinguishable downstream from a run that did the work.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app
from forge_api.settings import get_settings


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A stack with nothing configured — the reported starting state."""
    for var in (
        "FORGE_MODEL_PROVIDER",
        "FORGE_MODEL_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "FORGE_ALLOW_SCRIPTED_AGENT",
    ):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    with TestClient(app) as c:
        yield c


def _run(client: TestClient):
    return client.post(
        "/agent/runs",
        json={"objective": "Add a unique index to the onboarding table"},
    )


def test_a_run_is_refused_when_no_provider_is_configured(client: TestClient) -> None:
    resp = _run(client)

    assert resp.status_code == 503, resp.text
    assert resp.status_code != 200


def test_the_refusal_names_what_is_missing(client: TestClient) -> None:
    """A 503 the reader cannot act on is barely better than a wrong 200."""
    detail = _run(client).json()["detail"]

    assert "FORGE_MODEL_PROVIDER" in detail
    # The dev stack reads deploy/.env.dev, not the repo-root .env — the exact
    # trap that produces this state after following the getting-started guide.
    assert ".env.dev" in detail
    assert "FORGE_ALLOW_SCRIPTED_AGENT" in detail


def test_it_never_reports_success(client: TestClient) -> None:
    """The failure this exists to prevent."""
    body = _run(client).text

    assert '"succeeded"' not in body
    assert "0.9" not in body


def test_the_offline_stub_is_reachable_only_by_explicit_opt_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_ALLOW_SCRIPTED_AGENT", "1")
    get_settings.cache_clear()

    resp = _run(client)

    assert resp.status_code == 201, resp.text
    payload = resp.json()
    # It runs, but it says plainly that it did nothing...
    assert "no model was called" in payload["output"]
    # ...and claims no confidence in it.
    assert all(step.get("confidence") is None for step in payload["steps"])
