"""End-to-end OIDC authorization-code flow through the ASGI app (fully offline).

Mirrors ``test_saml_flow.py``: a locally-generated RSA keypair backs a fake IdP
that serves an OpenID-discovery document + JWKS over an injected
``httpx.MockTransport`` and signs real RS256 ID tokens, so the suite controls
every validated field with no live network. Covers the happy path
(login → callback JIT-provisions + sets the session cookie) and the rejection
matrix (tampered signature, expired, wrong audience, nonce/state mismatch) plus
groups → role mapping.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from conftest import WS_ID, Keypair, install_config  # noqa: F401  (shared fixtures)
from jwt import algorithms
from sqlalchemy import select

from forge_api.auth.service import get_auth_service
from forge_api.routers.oidc import get_oidc_client, get_oidc_state_store
from forge_api.sso.config_service import SsoConfigService
from forge_api.sso.oidc import InMemoryOidcStateStore, OidcClient
from forge_contracts.sso import OidcIdpConfig
from forge_db.models import ExternalIdentity, OidcConfiguration, User

CLIENT_ID = "forge-oidc-client"
CLIENT_SECRET = "s3cr3t-oidc-value"
CLIENT_SECRET_REF = "vault://oidc/acme/client_secret"
PUBLIC_URL = "http://localhost:8000"


class FakeOidcIdp:
    """A locally-signed OpenID Provider served over an httpx MockTransport."""

    def __init__(self, keypair: Keypair, *, kid: str = "oidc-test-key-1") -> None:
        self.keypair = keypair
        self.kid = kid
        self.issuer = "https://idp.oidc.example"
        self.discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        self.authorization_endpoint = f"{self.issuer}/authorize"
        self.token_endpoint = f"{self.issuer}/token"
        self.jwks_uri = f"{self.issuer}/jwks"
        self.next_id_token: str | None = None

    def jwks(self) -> dict:
        jwk = json.loads(algorithms.RSAAlgorithm.to_jwk(self.keypair.key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def issue_id_token(
        self,
        *,
        nonce: str,
        sub: str = "dana-oidc-sub",
        email: str = "dana@acme.com",
        name: str = "Dana Scully",
        aud: str | None = None,
        iss: str | None = None,
        exp: int | None = None,
        iat: int | None = None,
        groups: list[str] | None = None,
        sign_with: Keypair | None = None,
        kid: str | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict = {
            "iss": iss or self.issuer,
            "sub": sub,
            "aud": aud or CLIENT_ID,
            "exp": exp if exp is not None else now + 3600,
            "iat": iat if iat is not None else now,
            "nonce": nonce,
            "email": email,
            "name": name,
        }
        if groups is not None:
            claims["groups"] = groups
        keypair = sign_with or self.keypair
        return jwt.encode(
            claims, keypair.key_pem, algorithm="RS256", headers={"kid": kid or self.kid}
        )

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == self.discovery_url:
                return httpx.Response(
                    200,
                    json={
                        "issuer": self.issuer,
                        "authorization_endpoint": self.authorization_endpoint,
                        "token_endpoint": self.token_endpoint,
                        "jwks_uri": self.jwks_uri,
                    },
                )
            if url == self.jwks_uri:
                return httpx.Response(200, json=self.jwks())
            if url.startswith(self.token_endpoint):
                assert self.next_id_token is not None, "test did not stage an id_token"
                return httpx.Response(
                    200,
                    json={
                        "access_token": "opaque-access-token",
                        "token_type": "Bearer",
                        "id_token": self.next_id_token,
                    },
                )
            return httpx.Response(404, json={"error": "not_found", "url": url})  # pragma: no cover

        return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _isolate_oidc_caches():
    """Reset the process-wide discovery/JWKS caches between tests."""
    OidcClient.clear_caches()
    yield
    OidcClient.clear_caches()


@pytest.fixture
def idp(idp_keypair: Keypair) -> FakeOidcIdp:
    return FakeOidcIdp(idp_keypair)


def install_oidc_config(
    session_factory,
    *,
    issuer: str,
    workspace_id: uuid.UUID = WS_ID,
    enabled: bool = True,
    group_role_map: dict[str, str] | None = None,
    jit_provisioning: bool = True,
) -> OidcConfiguration:
    """Provision an OIDC config directly through the service layer (secret vaulted)."""
    with session_factory() as session:
        service = SsoConfigService(session, public_url=PUBLIC_URL)
        dto = OidcIdpConfig(
            issuer=issuer,
            client_id=CLIENT_ID,
            client_secret_ref=CLIENT_SECRET_REF,
            group_role_map=group_role_map or {},
        )
        config = service.put_oidc_config(
            workspace_id,
            dto,
            client_secret=CLIENT_SECRET,
            enabled=enabled,
            jit_provisioning=jit_provisioning,
        )
        session.commit()
        config_id = config.id
    with session_factory() as session:
        config = session.get(OidcConfiguration, config_id)
        session.expunge(config)
        return config


def _prepare_client(client_factory: Callable[..., object], idp: FakeOidcIdp):
    """Build an unauthenticated client with the OIDC deps bound to the fake IdP."""
    store = InMemoryOidcStateStore()
    client = client_factory(authenticated=False)
    client.app.dependency_overrides[get_oidc_client] = lambda: OidcClient(transport=idp.transport())
    client.app.dependency_overrides[get_oidc_state_store] = lambda: store
    return client


def _login(client, idp: FakeOidcIdp, next_path: str = "/board") -> dict[str, list[str]]:
    response = client.get(f"/auth/oidc/acme/login?next={next_path}")
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(idp.authorization_endpoint), location
    return parse_qs(urlparse(location).query)


class TestOidcLogin:
    def test_login_redirects_to_authorize_with_pkce(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        query = _login(client, idp)
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [CLIENT_ID]
        assert query["code_challenge_method"] == ["S256"]
        assert "openid" in query["scope"][0].split()
        assert query["state"][0] and query["nonce"][0] and query["code_challenge"][0]
        assert query["redirect_uri"] == ["http://localhost:8000/auth/oidc/acme/callback"]

    def test_unknown_realm_404(self, client_factory, session_factory, idp):
        client = _prepare_client(client_factory, idp)
        assert client.get("/auth/oidc/nope/login").status_code == 404

    def test_disabled_config_403(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer, enabled=False)
        client = _prepare_client(client_factory, idp)
        assert client.get("/auth/oidc/acme/login").status_code == 403


class TestOidcCallback:
    def test_happy_path_jit_and_cookie(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        query = _login(client, idp)
        idp.next_id_token = idp.issue_id_token(nonce=query["nonce"][0])

        response = client.get(
            f"/auth/oidc/acme/callback?code=auth-code-1&state={query['state'][0]}"
        )
        assert response.status_code == 302, response.text
        assert response.headers["location"] == "/board"
        assert "forge_session=" in response.headers.get("set-cookie", "")

        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "dana@acme.com")).scalar_one()
            assert user.workspace_id == WS_ID
            link = session.execute(
                select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)
            ).scalar_one()
            assert link.external_id == "dana-oidc-sub"
            assert link.provider.value == "oidc"

        # The minted cookie is a working Forge API credential.
        cookie = response.headers["set-cookie"].split("forge_session=")[1].split(";")[0]
        principal = get_auth_service().authenticate(cookie)
        assert principal.workspace_id == WS_ID

    def test_second_login_links_no_duplicate(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        for _ in range(2):
            query = _login(client, idp)
            idp.next_id_token = idp.issue_id_token(nonce=query["nonce"][0])
            resp = client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}")
            assert resp.status_code == 302
        with session_factory() as session:
            users = session.execute(select(User).where(User.email == "dana@acme.com")).all()
            assert len(users) == 1

    def test_tampered_signature_rejected(self, client_factory, session_factory, idp, wrong_keypair):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        query = _login(client, idp)
        # Signed by the wrong key but advertising the IdP's real kid → the JWKS
        # key is used and the RS256 signature check fails.
        idp.next_id_token = idp.issue_id_token(
            nonce=query["nonce"][0], sign_with=wrong_keypair, kid=idp.kid
        )
        response = client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}")
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_signature"
        with session_factory() as session:
            assert (
                session.execute(
                    select(User).where(User.email == "dana@acme.com")
                ).scalar_one_or_none()
                is None
            )

    def test_expired_token_rejected(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        query = _login(client, idp)
        past = int(time.time()) - 3600
        idp.next_id_token = idp.issue_id_token(nonce=query["nonce"][0], iat=past, exp=past + 60)
        response = client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}")
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "expired"

    def test_wrong_audience_rejected(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        query = _login(client, idp)
        idp.next_id_token = idp.issue_id_token(nonce=query["nonce"][0], aud="some-other-client")
        response = client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}")
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "audience_mismatch"

    def test_nonce_mismatch_rejected(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        query = _login(client, idp)
        idp.next_id_token = idp.issue_id_token(nonce="a-different-nonce")
        response = client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}")
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "nonce_mismatch"

    def test_unknown_state_rejected(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        _login(client, idp)  # establishes a real transaction, then we present a bogus state
        response = client.get("/auth/oidc/acme/callback?code=c&state=never-issued")
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_state"

    def test_state_is_single_use(self, client_factory, session_factory, idp):
        install_oidc_config(session_factory, issuer=idp.issuer)
        client = _prepare_client(client_factory, idp)
        query = _login(client, idp)
        idp.next_id_token = idp.issue_id_token(nonce=query["nonce"][0])
        first = client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}")
        assert first.status_code == 302
        replay = client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}")
        assert replay.status_code == 400
        assert replay.json()["detail"]["reason"] == "invalid_state"


class TestOidcGroupRoleMapping:
    def test_mapped_group_grants_admin_unmapped_stays_member(
        self, client_factory, session_factory, idp
    ):
        install_oidc_config(
            session_factory, issuer=idp.issuer, group_role_map={"forge-admins": "admin"}
        )
        client = _prepare_client(client_factory, idp)

        query = _login(client, idp)
        idp.next_id_token = idp.issue_id_token(
            nonce=query["nonce"][0],
            sub="lead-sub",
            email="lead@acme.com",
            groups=["forge-admins"],
        )
        assert (
            client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}").status_code
            == 302
        )

        query = _login(client, idp)
        idp.next_id_token = idp.issue_id_token(
            nonce=query["nonce"][0],
            sub="dev-sub",
            email="dev@acme.com",
            groups=["random-team"],
        )
        assert (
            client.get(f"/auth/oidc/acme/callback?code=c&state={query['state'][0]}").status_code
            == 302
        )

        with session_factory() as session:
            lead = session.execute(select(User).where(User.email == "lead@acme.com")).scalar_one()
            dev = session.execute(select(User).where(User.email == "dev@acme.com")).scalar_one()
            assert lead.role.value == "admin"
            assert dev.role.value == "member"
