"""End-to-end SAML flow through the ASGI app (AC1-AC8, AC10, AC22)."""

from __future__ import annotations

import base64
import zlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from conftest import (
    IDP_SSO_URL,
    WS_ID,
    Keypair,
    build_saml_response,
    encode_unsigned,
    install_config,
    sign_saml_response,
)
from lxml import etree
from sqlalchemy import select

from forge_api.auth.service import get_auth_service
from forge_db.models import AuditLog, ExternalIdentity, User


def _sp_login(client, next_path: str = "/board"):
    response = client.get(f"/auth/saml/acme/login?next={next_path}")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(IDP_SSO_URL)
    return parse_qs(urlparse(location).query)


def _request_id(query: dict) -> str:
    inflated = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15)
    return etree.fromstring(inflated).get("ID")


class TestSpMetadataEndpoint:
    def test_metadata_xml(self, client_factory, session_factory, idp_keypair: Keypair):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        response = client.get("/auth/saml/acme/metadata")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")
        root = etree.fromstring(response.content)
        md = "{urn:oasis:names:tc:SAML:2.0:metadata}"
        assert root.get("entityID").endswith("/auth/saml/acme/metadata")
        acs = root.find(f"{md}SPSSODescriptor/{md}AssertionConsumerService")
        assert acs.get("Location").endswith("/auth/saml/acme/acs")
        assert root.find(f"{md}SPSSODescriptor/{md}SingleLogoutService") is not None
        assert b"PRIVATE KEY" not in response.content

    def test_unknown_realm_404(self, client_factory, session_factory, idp_keypair):
        client = client_factory(authenticated=False)
        assert client.get("/auth/saml/nope/metadata").status_code == 404


class TestSpInitiatedLogin:
    def test_login_redirect_signed(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        query = _sp_login(client)
        assert query["RelayState"] == ["/board"]
        assert "SigAlg" in query and "Signature" in query
        assert _request_id(query).startswith("_")

    def test_acs_happy_jit_and_link(
        self, client_factory, session_factory, idp_keypair: Keypair
    ):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)

        # Round 1: SP-initiated login JIT-provisions dana.
        request_id = _request_id(_sp_login(client))
        b64 = sign_saml_response(
            build_saml_response(in_response_to=request_id), idp_keypair
        )
        response = client.post(
            "/auth/saml/acme/acs",
            data={"SAMLResponse": b64, "RelayState": "/board"},
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/board"
        assert "forge_session=" in response.headers.get("set-cookie", "")

        with session_factory() as session:
            user = session.execute(
                select(User).where(User.email == "dana@acme.com")
            ).scalar_one()
            assert user.workspace_id == WS_ID
            link = session.execute(
                select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)
            ).scalar_one()
            assert link.external_id == "dana@acme.com"

        # The minted session cookie is a working Forge API credential.
        cookie = response.headers["set-cookie"].split("forge_session=")[1].split(";")[0]
        principal = get_auth_service().authenticate(cookie)
        assert principal.workspace_id == WS_ID

        # Round 2: same NameID links (no duplicate user).
        request_id = _request_id(_sp_login(client))
        b64 = sign_saml_response(
            build_saml_response(in_response_to=request_id), idp_keypair
        )
        assert client.post(
            "/auth/saml/acme/acs", data={"SAMLResponse": b64}
        ).status_code == 302
        with session_factory() as session:
            users = session.execute(
                select(User).where(User.email == "dana@acme.com")
            ).all()
            assert len(users) == 1

    def test_acs_unsigned_400_no_user(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        request_id = _request_id(_sp_login(client))
        b64 = encode_unsigned(build_saml_response(in_response_to=request_id))
        response = client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "unsigned"
        with session_factory() as session:
            assert (
                session.execute(
                    select(User).where(User.email == "dana@acme.com")
                ).scalar_one_or_none()
                is None
            )

    def test_acs_expired_400(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        request_id = _request_id(_sp_login(client))
        b64 = sign_saml_response(
            build_saml_response(
                in_response_to=request_id,
                not_on_or_after=datetime.now(UTC) - timedelta(minutes=30),
            ),
            idp_keypair,
        )
        response = client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "expired"

    def test_acs_audience_mismatch_400(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        request_id = _request_id(_sp_login(client))
        b64 = sign_saml_response(
            build_saml_response(
                in_response_to=request_id, audience="https://other-sp.example"
            ),
            idp_keypair,
        )
        response = client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "audience_mismatch"

    def test_acs_replay_rejected(self, client_factory, session_factory, idp_keypair):
        """AC7: a previously-accepted assertion (and its InResponseTo) is one-shot."""
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        request_id = _request_id(_sp_login(client))
        b64 = sign_saml_response(
            build_saml_response(in_response_to=request_id), idp_keypair
        )
        assert client.post(
            "/auth/saml/acme/acs", data={"SAMLResponse": b64}
        ).status_code == 302
        replayed = client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert replayed.status_code == 400
        assert replayed.json()["detail"]["reason"] == "unknown_in_response_to"

    def test_acs_unknown_in_response_to_rejected(
        self, client_factory, session_factory, idp_keypair
    ):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        b64 = sign_saml_response(
            build_saml_response(in_response_to="_never_issued"), idp_keypair
        )
        response = client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "unknown_in_response_to"


class TestIdpInitiated:
    def test_allowed_when_flag_on(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair, allow_idp_initiated=True)
        client = client_factory(authenticated=False)
        b64 = sign_saml_response(build_saml_response(), idp_keypair)
        response = client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert response.status_code == 302

    def test_rejected_when_flag_off(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair, allow_idp_initiated=False)
        client = client_factory(authenticated=False)
        b64 = sign_saml_response(build_saml_response(), idp_keypair)
        response = client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "idp_initiated_disabled"


class TestGroupRoleMappingViaAcs:
    def test_mapped_group_grants_admin_unmapped_does_not(
        self, client_factory, session_factory, idp_keypair
    ):
        """AC9 at the flow level: only group_role_map grants privilege."""
        install_config(
            session_factory,
            idp_keypair,
            allow_idp_initiated=True,
            attribute_mapping={"email": "", "groups": "groups"},
            group_role_map={"forge-admins": "admin"},
        )
        client = client_factory(authenticated=False)
        b64 = sign_saml_response(
            build_saml_response(
                name_id="lead@acme.com", attributes={"groups": ["forge-admins"]}
            ),
            idp_keypair,
        )
        assert client.post(
            "/auth/saml/acme/acs", data={"SAMLResponse": b64}
        ).status_code == 302
        b64 = sign_saml_response(
            build_saml_response(
                name_id="dev@acme.com",
                attributes={"groups": ["random"], "role": ["admin"]},
            ),
            idp_keypair,
        )
        assert client.post(
            "/auth/saml/acme/acs", data={"SAMLResponse": b64}
        ).status_code == 302
        with session_factory() as session:
            lead = session.execute(
                select(User).where(User.email == "lead@acme.com")
            ).scalar_one()
            dev = session.execute(
                select(User).where(User.email == "dev@acme.com")
            ).scalar_one()
            assert lead.role.value == "admin"
            assert dev.role.value == "member"


class TestDiscovery:
    def test_discover_routes_known_domain(
        self, client_factory, session_factory, idp_keypair
    ):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        response = client.post(
            "/auth/saml/discover", json={"email": "dana@acme.com"}
        )
        assert response.status_code == 200
        assert response.json() == {"sso": True, "redirect": "/auth/saml/acme/login"}

    def test_discover_unknown_domain(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        response = client.post(
            "/auth/saml/discover", json={"email": "who@unknown.example"}
        )
        assert response.json() == {"sso": False, "redirect": None}

    def test_discover_disabled_config_is_not_routed(
        self, client_factory, session_factory, idp_keypair
    ):
        install_config(session_factory, idp_keypair, enabled=False)
        client = client_factory(authenticated=False)
        response = client.post("/auth/saml/discover", json={"email": "dana@acme.com"})
        assert response.json()["sso"] is False


class TestAuditAndNonRegression:
    def test_login_audits_success_and_failure(
        self, client_factory, session_factory, idp_keypair
    ):
        install_config(session_factory, idp_keypair, allow_idp_initiated=True)
        client = client_factory(authenticated=False)
        good = sign_saml_response(build_saml_response(), idp_keypair)
        assert client.post(
            "/auth/saml/acme/acs", data={"SAMLResponse": good}
        ).status_code == 302
        bad = encode_unsigned(build_saml_response())
        assert client.post(
            "/auth/saml/acme/acs", data={"SAMLResponse": bad}
        ).status_code == 400
        with session_factory() as session:
            rows = session.execute(
                select(AuditLog.action, AuditLog.result).where(
                    AuditLog.workspace_id == WS_ID
                )
            ).all()
            actions = [a for a, _ in rows]
            assert "sso.login" in actions
            assert "sso.login_failed" in actions
            assert ("sso.login_failed", "failure") in rows

    def test_oauth_login_route_still_works_with_sso_enabled(
        self, client_factory, session_factory, idp_keypair, monkeypatch
    ):
        """AC22: the V1 OAuth path is unaffected by an enabled SAML config."""
        monkeypatch.setenv("FORGE_OAUTH_GITHUB_CLIENT_ID", "cid")
        monkeypatch.setenv("FORGE_OAUTH_GITHUB_CLIENT_SECRET", "sec")
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        response = client.post("/auth/login", json={"provider": "github"})
        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "github"
        assert body["authorize_url"].startswith("https://")

    def test_slo_endpoint_is_honestly_parked(
        self, client_factory, session_factory, idp_keypair
    ):
        install_config(session_factory, idp_keypair)
        client = client_factory(authenticated=False)
        assert client.get("/auth/saml/acme/slo").status_code == 501
