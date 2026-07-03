"""Admin SSO config + SCIM token routes (AC10, AC17, AC18 partial, RBAC)."""

from __future__ import annotations

from conftest import (
    OTHER_WS_ID,
    WS_ID,
    Keypair,
    build_saml_response,
    install_config,
    make_config_in,
    make_idp_metadata,
    sign_saml_response,
)
from sqlalchemy import select

from forge_contracts import UserRole
from forge_db.models import AuditLog, User


class TestConfigCrud:
    def test_put_and_get_roundtrip_no_private_key(
        self, client_factory, session_factory, idp_keypair: Keypair
    ):
        admin = client_factory(UserRole.ADMIN)
        response = admin.put(
            f"/workspaces/{WS_ID}/sso", json=make_config_in(idp_keypair)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is True
        assert body["sp_entity_id"].endswith("/auth/saml/acme/metadata")
        assert body["sp_acs_url"].endswith("/auth/saml/acme/acs")
        assert body["domains"] == ["acme.com"]
        assert "BEGIN CERTIFICATE" in body["sp_cert_pem"]
        # AC18: the SP private key never leaves the server in any shape.
        assert "PRIVATE KEY" not in response.text
        assert "sp_private_key" not in response.text

        fetched = admin.get(f"/workspaces/{WS_ID}/sso")
        assert fetched.status_code == 200
        assert fetched.json()["idp"]["entity_id"] == body["idp"]["entity_id"]
        assert "PRIVATE KEY" not in fetched.text

    def test_put_from_metadata_xml(self, client_factory, idp_keypair: Keypair):
        admin = client_factory(UserRole.ADMIN)
        payload = make_config_in(idp_keypair)
        payload["idp"] = None
        payload["metadata_xml"] = make_idp_metadata(idp_keypair)
        response = admin.put(f"/workspaces/{WS_ID}/sso", json=payload)
        assert response.status_code == 200
        assert response.json()["idp"]["sso_url"] == "https://idp.acme-corp.example/sso"

    def test_get_missing_404(self, client_factory):
        admin = client_factory(UserRole.ADMIN)
        assert admin.get(f"/workspaces/{WS_ID}/sso").status_code == 404

    def test_domain_conflict_409(self, client_factory, session_factory, idp_keypair):
        """AC10: a domain bound to one workspace cannot be claimed by another."""
        install_config(session_factory, idp_keypair)  # acme owns acme.com
        other_admin = client_factory(UserRole.ADMIN, workspace_id=OTHER_WS_ID)
        response = other_admin.put(
            f"/workspaces/{OTHER_WS_ID}/sso", json=make_config_in(idp_keypair)
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "domain_conflict"
        assert response.json()["detail"]["domain"] == "acme.com"

    def test_member_cannot_configure_sso(self, client_factory, idp_keypair):
        member = client_factory(UserRole.MEMBER)
        assert member.get(f"/workspaces/{WS_ID}/sso").status_code == 403
        assert (
            member.put(
                f"/workspaces/{WS_ID}/sso", json=make_config_in(idp_keypair)
            ).status_code
            == 403
        )
        assert member.get(f"/workspaces/{WS_ID}/scim/tokens").status_code == 403

    def test_foreign_workspace_404(self, client_factory, idp_keypair):
        admin = client_factory(UserRole.ADMIN)  # acme admin
        assert admin.get(f"/workspaces/{OTHER_WS_ID}/sso").status_code == 404

    def test_unauthenticated_401(self, client_factory):
        anon = client_factory(authenticated=False)
        assert anon.get(f"/workspaces/{WS_ID}/sso").status_code == 401

    def test_delete_config(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair)
        admin = client_factory(UserRole.ADMIN)
        assert admin.delete(f"/workspaces/{WS_ID}/sso").status_code == 204
        assert admin.get(f"/workspaces/{WS_ID}/sso").status_code == 404
        # Domain freed: the other workspace can now claim it.
        other_admin = client_factory(UserRole.ADMIN, workspace_id=OTHER_WS_ID)
        payload = make_config_in(idp_keypair)
        response = other_admin.put(f"/workspaces/{OTHER_WS_ID}/sso", json=payload)
        assert response.status_code == 200


class TestEnableDisable:
    def test_disable_then_enable(self, client_factory, session_factory, idp_keypair):
        install_config(session_factory, idp_keypair)
        admin = client_factory(UserRole.ADMIN)
        disabled = admin.post(f"/workspaces/{WS_ID}/sso/disable")
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        enabled = admin.post(f"/workspaces/{WS_ID}/sso/enable")
        assert enabled.json()["enabled"] is True

    def test_disable_blocked_without_local_admin(
        self, client_factory, session_factory, idp_keypair
    ):
        """AC17: disabling SSO with no break-glass local admin → 409 last_admin."""
        install_config(session_factory, idp_keypair)
        with session_factory() as session:
            for user in (
                session.execute(
                    select(User).where(
                        User.workspace_id == WS_ID, User.role == UserRole.ADMIN
                    )
                )
                .scalars()
                .all()
            ):
                user.external_managed = True  # directory-owned: not break-glass
            session.commit()
        admin = client_factory(UserRole.ADMIN)
        response = admin.post(f"/workspaces/{WS_ID}/sso/disable")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "last_admin"


class TestConnectionTest:
    def test_validation_only_no_session_no_user(
        self, client_factory, session_factory, idp_keypair
    ):
        install_config(session_factory, idp_keypair)
        admin = client_factory(UserRole.ADMIN)
        b64 = sign_saml_response(
            build_saml_response(attributes={"groups": ["eng"]}), idp_keypair
        )
        response = admin.post(
            f"/workspaces/{WS_ID}/sso/test", json={"saml_response": b64}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name_id"] == "dana@acme.com"
        assert body["attributes"] == {"groups": ["eng"]}
        assert "set-cookie" not in response.headers
        with session_factory() as session:
            assert (
                session.execute(
                    select(User).where(User.email == "dana@acme.com")
                ).scalar_one_or_none()
                is None
            )

    def test_invalid_response_reports_reason(
        self, client_factory, session_factory, idp_keypair, wrong_keypair
    ):
        install_config(session_factory, idp_keypair)
        admin = client_factory(UserRole.ADMIN)
        b64 = sign_saml_response(build_saml_response(), wrong_keypair)
        response = admin.post(
            f"/workspaces/{WS_ID}/sso/test", json={"saml_response": b64}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "bad_signature"


class TestScimTokens:
    def test_token_lifecycle_and_one_time_reveal(
        self, client_factory, session_factory, idp_keypair
    ):
        admin = client_factory(UserRole.ADMIN)
        created = admin.post(
            f"/workspaces/{WS_ID}/scim/tokens", json={"name": "Okta production"}
        )
        assert created.status_code == 201
        body = created.json()
        raw = body["token"]
        assert raw.startswith("forge_scim_")
        assert body["token_prefix"] == raw[:8]

        listed = admin.get(f"/workspaces/{WS_ID}/scim/tokens")
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        # The raw token appears exactly once (mint response), never in lists.
        assert raw not in listed.text
        assert "token_hash" not in listed.text

        token_id = rows[0]["id"]
        assert (
            admin.delete(f"/workspaces/{WS_ID}/scim/tokens/{token_id}").status_code
            == 204
        )
        assert admin.get(f"/workspaces/{WS_ID}/scim/tokens").json()[0]["revoked_at"]

        # Audit trail for issue + revoke, with no raw token in the details.
        with session_factory() as session:
            rows = session.execute(
                select(AuditLog).where(AuditLog.workspace_id == WS_ID)
            ).scalars().all()
            actions = [row.action for row in rows]
            assert "scim.token_issued" in actions
            assert "scim.token_revoked" in actions
            for row in rows:
                assert raw not in str(row.details)

    def test_duplicate_name_409(self, client_factory):
        admin = client_factory(UserRole.ADMIN)
        assert (
            admin.post(
                f"/workspaces/{WS_ID}/scim/tokens", json={"name": "dup"}
            ).status_code
            == 201
        )
        assert (
            admin.post(
                f"/workspaces/{WS_ID}/scim/tokens", json={"name": "dup"}
            ).status_code
            == 409
        )
