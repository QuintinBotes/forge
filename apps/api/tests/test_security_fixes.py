"""Reproduction + regression tests for the Phase-2 round-1 security fixes.

Covers three of the four defects fixed in Task 2.3-fix-r1:

* real authentication is wired into ``get_current_principal`` (no hardcoded admin)
  so every feature router rejects unauthenticated callers (401);
* the CORS default is locked down and a wildcard origin is never paired with
  credentials (a credential-leak / reflected-origin vector).

(Webhook signature verification lives in ``test_integration_router.py`` and
cross-tenant knowledge isolation in ``test_knowledge_api.py``.)
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from forge_api.deps import get_current_principal
from forge_api.main import create_app
from forge_api.settings import Settings

# --------------------------------------------------------------------------- #
# Authentication is wired (Task 1.15) — no anonymous admin                     #
# --------------------------------------------------------------------------- #


def test_get_current_principal_rejects_missing_credentials() -> None:
    # The stub admin principal is gone: with no credentials the dependency must
    # raise 401 rather than return a full-scope admin.
    with pytest.raises(HTTPException) as exc:
        get_current_principal()
    assert exc.value.status_code == 401


def test_feature_router_requires_authentication() -> None:
    # A feature endpoint hit with no Authorization header must be rejected.
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/knowledge/search", json={"query": "anything", "k": 3}
        )
    assert resp.status_code == 401


def test_invalid_api_key_is_rejected() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get(
            "/board/tasks",
            headers={"Authorization": "Bearer forge_not_a_real_key"},
        )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# CORS hardening (insecure-default fix)                                        #
# --------------------------------------------------------------------------- #


def test_default_cors_has_no_wildcard_origin() -> None:
    settings = Settings()
    assert "*" not in settings.cors_origins


def test_wildcard_origin_is_never_paired_with_credentials() -> None:
    # Even if a deployment misconfigures wildcard origins *with* credentials, the
    # app must not reflect an arbitrary origin back with credentials enabled.
    settings = Settings(cors_origins=["*"], cors_allow_credentials=True)
    app = create_app(settings)
    with TestClient(app) as client:
        resp = client.get(
            "/health", headers={"Origin": "https://evil.example"}
        )
    acao = resp.headers.get("access-control-allow-origin")
    acac = resp.headers.get("access-control-allow-credentials")
    # The arbitrary origin must not be reflected, and "*" must never be paired
    # with credentials.
    assert acao != "https://evil.example"
    assert not (acao == "*" and acac == "true")


def test_explicit_origin_with_credentials_still_works() -> None:
    settings = Settings(
        cors_origins=["https://app.forge.local"], cors_allow_credentials=True
    )
    app = create_app(settings)
    with TestClient(app) as client:
        resp = client.get(
            "/health", headers={"Origin": "https://app.forge.local"}
        )
    assert resp.headers.get("access-control-allow-origin") == "https://app.forge.local"
    assert resp.headers.get("access-control-allow-credentials") == "true"
