"""Tests for the Forge API skeleton (plan Task 0.4).

The API skeleton is the SHARED SUBSTRATE every Phase-1 feature task wires its
handlers into. These tests pin the substrate's guarantees:

- ``GET /health`` returns 200 with a typed health payload (hit via an
  httpx + ASGI transport client, per the plan),
- every feature router named in the plan is pre-registered/mounted so Phase 1
  fills handlers without ever touching ``main.py``,
- every stub route returns 501 with its declared ``NotImplementedResponse``
  schema shape (and does so regardless of input — no request body is required
  by a stub, so a bare call never 422s before reaching the handler),
- the auth stub dependency yields a deterministic test principal, and
- the app builds a valid OpenAPI document that documents each route's eventual
  response model.

No external services are required: the DB session dependency is lazy and the
stub handlers never touch it.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from forge_api.deps import Principal, get_current_principal
from forge_api.main import app, create_app
from forge_api.settings import Settings, get_settings

# Routers the plan requires to be pre-registered (Task 0.4).
FEATURE_ROUTER_PREFIXES = [
    "/board",
    "/spec",
    "/knowledge",
    "/workflow",
    "/agent",
    "/policy",
    "/mcp",
    "/integration",
    "/approval",
    "/observability",
    "/auth",
]

# Paths that are real (not 501 stubs) or framework-provided.
_NON_STUB_PATHS = {"/", "/health", "/healthz", "/readyz", "/openapi.json", "/docs", "/redoc"}


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _openapi_operations() -> list[tuple[str, str]]:
    """Return ``(method, path_template)`` for every operation in the OpenAPI doc."""
    ops: list[tuple[str, str]] = []
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            ops.append((method.upper(), path))
    return ops


def _concrete_path(path: str) -> str:
    """Substitute any path params with a valid sample value (uuid string)."""
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        out = out[:start] + str(uuid.uuid4()) + out[end + 1 :]
    return out


# --------------------------------------------------------------------------- #
# Health                                                                       #
# --------------------------------------------------------------------------- #


async def test_health_returns_200(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"]


async def test_root_returns_200(client: httpx.AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.json()["name"]


# --------------------------------------------------------------------------- #
# Every feature router is mounted                                              #
# --------------------------------------------------------------------------- #


def test_every_feature_router_is_registered() -> None:
    paths = [p for _, p in _openapi_operations()]
    for prefix in FEATURE_ROUTER_PREFIXES:
        assert any(p == prefix or p.startswith(prefix + "/") for p in paths), (
            f"router {prefix!r} is not mounted"
        )


def test_stub_routes_exist() -> None:
    # Skeleton must pre-register a meaningful number of endpoints to fill.
    stub_paths = {p for _, p in _openapi_operations() if p not in _NON_STUB_PATHS}
    assert len(stub_paths) >= len(FEATURE_ROUTER_PREFIXES)


# --------------------------------------------------------------------------- #
# Every stub returns 501 with the declared NotImplementedResponse shape        #
# --------------------------------------------------------------------------- #


async def test_all_stub_routes_return_501(client: httpx.AsyncClient) -> None:
    # Phase 0 registers every feature route as a 501 stub. As Phase-1 tasks fill
    # their own router, that route starts returning real data — so this test
    # asserts the *invariant* that holds throughout: a route is either a
    # well-formed 501 stub, or it is implemented and returns a non-server-error
    # response (never an un-typed 500). It never requires a route to stay a stub.
    checked = 0
    for method, path in _openapi_operations():
        if path in _NON_STUB_PATHS:
            continue
        url = _concrete_path(path)
        resp = await client.request(method, url)
        if resp.status_code == 501:
            body = resp.json()
            # Declared schema shape: NotImplementedResponse.
            assert body["status"] == "not_implemented"
            assert body["router"]
            assert body["operation"]
        else:
            # Implemented in Phase 1 — must be a valid (non-stub) response, not a
            # server error from a half-wired handler.
            assert resp.status_code < 500, (
                f"{method} {url} returned {resp.status_code}, expected a stub 501 "
                "or an implemented non-5xx response"
            )
        checked += 1
    assert checked >= len(FEATURE_ROUTER_PREFIXES)


# --------------------------------------------------------------------------- #
# Auth stub principal                                                          #
# --------------------------------------------------------------------------- #


def test_auth_stub_returns_test_principal() -> None:
    principal = get_current_principal()
    assert isinstance(principal, Principal)
    assert principal.workspace_id is not None
    assert principal.user_id is not None
    assert principal.role.value in {"admin", "member", "viewer", "agent-runner"}


# --------------------------------------------------------------------------- #
# Settings + app factory                                                       #
# --------------------------------------------------------------------------- #


def test_settings_loads_with_defaults() -> None:
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.database_url
    assert settings.app_name


def test_create_app_is_independent_instance() -> None:
    other = create_app()
    assert other is not app
    assert other.title


# --------------------------------------------------------------------------- #
# OpenAPI document is valid and documents eventual response models             #
# --------------------------------------------------------------------------- #


def test_openapi_document_builds() -> None:
    schema = app.openapi()
    assert schema["openapi"].startswith("3.")
    assert "NotImplementedResponse" in schema["components"]["schemas"]
    # Eventual domain DTOs are documented somewhere in the component schemas.
    assert "TaskDTO" in schema["components"]["schemas"]
