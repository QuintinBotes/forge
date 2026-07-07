"""Tests for the OAuth authorization-code exchange (Task H4 — auth hardening).

Drives the whole code -> tokens -> user flow against a mocked IdP via
:class:`httpx.MockTransport`; no real network call is ever made. Covers the
happy path for every V1 provider plus the failure paths (IdP rejects the code,
no access token, userinfo failure, unknown provider, unconfigured credentials,
and state/CSRF mismatch).
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable

import httpx
import pytest

from forge_api.auth.oauth import (
    DEFAULT_PROVIDERS,
    OAuthClient,
    OAuthClientCredentials,
    OAuthConfigError,
    OAuthExchangeError,
    OAuthStateError,
    UnsupportedOAuthProviderError,
)

# -- mock IdP ---------------------------------------------------------------- #

# Per-provider canned userinfo payloads (the subject field differs per provider).
USERINFO: dict[str, dict[str, object]] = {
    "google": {"sub": "google-123", "email": "alice@example.com", "name": "Alice"},
    "github": {"id": 4242, "login": "alice", "name": "Alice GH", "email": "a@gh.com"},
    "gitlab": {"id": 99, "username": "alice", "name": "Alice GL", "email": "a@gl.com"},
}

TOKEN_PATHS = {p: urllib.parse.urlsplit(cfg.token_url).path for p, cfg in DEFAULT_PROVIDERS.items()}
USERINFO_PATHS = {
    p: urllib.parse.urlsplit(cfg.userinfo_url).path for p, cfg in DEFAULT_PROVIDERS.items()
}


def _make_handler(
    *,
    token_status: int = 200,
    token_body: dict[str, object] | None = None,
    userinfo_status: int = 200,
    capture: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler routing token/userinfo by URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        path = urllib.parse.urlsplit(str(request.url)).path
        for provider, token_path in TOKEN_PATHS.items():
            if path == token_path:
                body = (
                    token_body
                    if token_body is not None
                    else {
                        "access_token": f"at-{provider}",
                        "token_type": "bearer",
                        "scope": "read",
                        "expires_in": 3600,
                    }
                )
                return httpx.Response(token_status, json=body)
        for provider, ui_path in USERINFO_PATHS.items():
            if path == ui_path:
                return httpx.Response(userinfo_status, json=USERINFO[provider])
        return httpx.Response(404, json={"error": "unexpected path"})

    return handler


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OAuthClient:
    return OAuthClient(
        credentials={
            p: OAuthClientCredentials(client_id=f"id-{p}", client_secret=f"sec-{p}")
            for p in DEFAULT_PROVIDERS
        },
        transport=httpx.MockTransport(handler),
    )


# -- happy path -------------------------------------------------------------- #


@pytest.mark.parametrize("provider", sorted(DEFAULT_PROVIDERS))
async def test_complete_flow_resolves_user(provider: str) -> None:
    captured: list[httpx.Request] = []
    client = _client(_make_handler(capture=captured))

    result = await client.complete(provider, "the-code", redirect_uri="https://app/cb")

    assert result.provider == provider
    assert result.user.provider == provider
    assert result.user.subject == str(USERINFO[provider][DEFAULT_PROVIDERS[provider].subject_field])
    assert result.user.email == USERINFO[provider]["email"]
    assert result.user.name  # a display name was resolved
    assert result.tokens.access_token == f"at-{provider}"
    assert result.tokens.expires_in == 3600

    # The token request carried the auth-code grant + our client credentials.
    token_req = next(
        r for r in captured if urllib.parse.urlsplit(str(r.url)).path == TOKEN_PATHS[provider]
    )
    form = dict(urllib.parse.parse_qsl(token_req.content.decode()))
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["client_id"] == f"id-{provider}"
    assert form["client_secret"] == f"sec-{provider}"
    assert form["redirect_uri"] == "https://app/cb"

    # The userinfo request was bearer-authenticated with the access token.
    ui_req = next(
        r for r in captured if urllib.parse.urlsplit(str(r.url)).path == USERINFO_PATHS[provider]
    )
    assert ui_req.headers["Authorization"] == f"Bearer at-{provider}"


async def test_github_subject_is_stringified_numeric_id() -> None:
    client = _client(_make_handler())
    result = await client.complete("github", "c")
    assert result.user.subject == "4242"


async def test_form_encoded_token_response_is_accepted() -> None:
    """GitHub returns x-www-form-urlencoded by default; tolerate it."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = urllib.parse.urlsplit(str(request.url)).path
        if path == TOKEN_PATHS["github"]:
            return httpx.Response(
                200,
                text="access_token=gh-form-token&token_type=bearer&scope=read%3Auser",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        return httpx.Response(200, json=USERINFO["github"])

    client = _client(handler)
    result = await client.complete("github", "c")
    assert result.tokens.access_token == "gh-form-token"
    assert result.tokens.scope == "read:user"


# -- failure paths ----------------------------------------------------------- #


async def test_provider_rejects_authorization_code() -> None:
    client = _client(_make_handler(token_status=400, token_body={"error": "invalid_grant"}))
    with pytest.raises(OAuthExchangeError):
        await client.complete("google", "bad-code")


async def test_token_response_without_access_token() -> None:
    client = _client(_make_handler(token_body={"token_type": "bearer"}))
    with pytest.raises(OAuthExchangeError):
        await client.exchange_code("google", "c")


async def test_token_response_with_error_field_even_on_200() -> None:
    client = _client(_make_handler(token_body={"error": "invalid_client"}))
    with pytest.raises(OAuthExchangeError):
        await client.exchange_code("github", "c")


async def test_userinfo_failure_raises() -> None:
    client = _client(_make_handler(userinfo_status=401))
    with pytest.raises(OAuthExchangeError):
        await client.complete("gitlab", "c")


async def test_userinfo_without_subject_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = urllib.parse.urlsplit(str(request.url)).path
        if path == TOKEN_PATHS["google"]:
            return httpx.Response(200, json={"access_token": "at", "token_type": "bearer"})
        return httpx.Response(200, json={"email": "no-sub@example.com"})

    client = _client(handler)
    with pytest.raises(OAuthExchangeError):
        await client.complete("google", "c")


async def test_unknown_provider_raises() -> None:
    client = _client(_make_handler())
    with pytest.raises(UnsupportedOAuthProviderError):
        await client.complete("facebook", "c")


async def test_missing_credentials_raises_config_error() -> None:
    # Valid provider, but no client credentials configured for it.
    client = OAuthClient(credentials={}, transport=httpx.MockTransport(_make_handler()))
    with pytest.raises(OAuthConfigError):
        await client.exchange_code("google", "c")


async def test_state_mismatch_raises_before_any_network_call() -> None:
    calls: list[httpx.Request] = []
    client = _client(_make_handler(capture=calls))
    with pytest.raises(OAuthStateError):
        await client.complete("google", "c", state="returned", expected_state="issued")
    assert calls == []  # no exchange attempted on CSRF failure


async def test_matching_state_completes() -> None:
    client = _client(_make_handler())
    result = await client.complete("google", "c", state="same-state", expected_state="same-state")
    assert result.user.subject == "google-123"


async def test_network_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    with pytest.raises(OAuthExchangeError):
        await client.exchange_code("google", "c")


# -- from_env ---------------------------------------------------------------- #


def test_from_env_loads_only_fully_configured_providers() -> None:
    env = {
        "FORGE_OAUTH_GOOGLE_CLIENT_ID": "g-id",
        "FORGE_OAUTH_GOOGLE_CLIENT_SECRET": "g-sec",
        "FORGE_OAUTH_GITHUB_CLIENT_ID": "gh-id-only",  # secret missing -> skipped
    }
    client = OAuthClient.from_env(env)
    assert set(client.credentials) == {"google"}
    assert client.credentials["google"].client_id == "g-id"


def test_from_env_empty_constructs_cleanly() -> None:
    client = OAuthClient.from_env({})
    assert client.credentials == {}
    # Provider config is still available even with no credentials.
    assert client.provider_config("github").name == "github"


def test_userinfo_payloads_serialise() -> None:
    # Guard against accidentally putting non-JSON objects in the fixtures.
    json.dumps(USERINFO)
