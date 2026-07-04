"""Shared test helpers for the integration SDK.

All HTTP interaction is routed through ``httpx.MockTransport`` against recorded
JSON fixtures — no live network calls are ever made (plan Task 1.13 + Global
Constraint: no real external API calls).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded JSON fixture by file name (with or without ``.json``)."""
    if not name.endswith(".json"):
        name = f"{name}.json"
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def load() -> Callable[[str], dict[str, Any]]:
    return load_fixture


def make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    """Wrap a request handler in an ``httpx.MockTransport``."""
    return httpx.MockTransport(handler)


class RequestRecorder:
    """Captures every request a client makes so tests can assert on payloads."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def by_path(self, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in r.url.path]


@pytest.fixture(autouse=True)
def _no_real_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove the hermetic lane makes zero real network calls (HARD-01 AC14).

    Every unit test here drives ``httpx`` through an injected ``MockTransport``,
    so the real ``HTTPTransport`` must never be reached. We monkeypatch its
    request handlers to raise, turning any accidental live call into a loud test
    failure. The creds-gated ``live_github`` tests opt out (they DO hit the
    network on purpose).
    """
    if request.node.get_closest_marker("live_github"):
        return

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "network access is disabled in the hermetic test lane "
            "(use httpx.MockTransport); a real HTTP call was attempted"
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked, raising=True)
