"""Admin OIDC config routes (GET/PUT /workspaces/{id}/oidc): CRUD + RBAC.

Mirrors ``test_sso_admin_api.py``'s ``TestConfigCrud`` for the dedicated OIDC
admin surface: a full round trip with no secret material ever serialized, the
"omit client_secret to keep the existing one" update affordance, RBAC, and
tenant isolation.
"""

from __future__ import annotations

from conftest import OTHER_WS_ID, WS_ID, make_oidc_config_in
from sqlalchemy import select

from forge_contracts import UserRole
from forge_db.models import OidcConfiguration


class TestOidcConfigCrud:
    def test_put_and_get_roundtrip_no_secret_material(self, client_factory):
        admin = client_factory(UserRole.ADMIN)
        response = admin.put(f"/workspaces/{WS_ID}/oidc", json=make_oidc_config_in())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is True
        assert body["issuer"] == "https://idp.oidc.example"
        assert body["client_id"] == "forge-oidc-client"
        assert body["has_client_secret"] is True
        assert body["redirect_uri"] == "http://localhost:8000/auth/oidc/acme/callback"
        assert body["login_url"] == "http://localhost:8000/auth/oidc/acme/login"
        # The plaintext secret never leaves the server in any shape.
        assert "client_secret" not in body
        assert "s3cr3t-oidc-value" not in response.text

        fetched = admin.get(f"/workspaces/{WS_ID}/oidc")
        assert fetched.status_code == 200
        assert fetched.json()["client_id"] == "forge-oidc-client"
        assert "s3cr3t-oidc-value" not in fetched.text

    def test_get_missing_404(self, client_factory):
        admin = client_factory(UserRole.ADMIN)
        assert admin.get(f"/workspaces/{WS_ID}/oidc").status_code == 404

    def test_update_without_client_secret_keeps_existing_secret(self, client_factory):
        admin = client_factory(UserRole.ADMIN)
        admin.put(f"/workspaces/{WS_ID}/oidc", json=make_oidc_config_in())
        payload = make_oidc_config_in(client_secret=None, issuer="https://idp2.oidc.example")
        response = admin.put(f"/workspaces/{WS_ID}/oidc", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["issuer"] == "https://idp2.oidc.example"
        assert response.json()["has_client_secret"] is True

    def test_first_save_without_client_secret_400(self, client_factory):
        admin = client_factory(UserRole.ADMIN)
        payload = make_oidc_config_in(client_secret=None)
        response = admin.put(f"/workspaces/{WS_ID}/oidc", json=payload)
        assert response.status_code == 400

    def test_member_cannot_configure_oidc(self, client_factory):
        member = client_factory(UserRole.MEMBER)
        assert member.get(f"/workspaces/{WS_ID}/oidc").status_code == 403
        assert (
            member.put(f"/workspaces/{WS_ID}/oidc", json=make_oidc_config_in()).status_code == 403
        )

    def test_foreign_workspace_404(self, client_factory):
        admin = client_factory(UserRole.ADMIN)  # acme admin
        assert admin.get(f"/workspaces/{OTHER_WS_ID}/oidc").status_code == 404

    def test_unauthenticated_401(self, client_factory):
        anon = client_factory(authenticated=False)
        assert anon.get(f"/workspaces/{WS_ID}/oidc").status_code == 401

    def test_vault_ref_generated_once_and_stable_across_updates(
        self, client_factory, session_factory
    ):
        admin = client_factory(UserRole.ADMIN)
        admin.put(f"/workspaces/{WS_ID}/oidc", json=make_oidc_config_in())
        rotate = make_oidc_config_in(client_secret=None, client_id="rotated-client")
        admin.put(f"/workspaces/{WS_ID}/oidc", json=rotate)
        with session_factory() as session:
            config = session.execute(
                select(OidcConfiguration).where(OidcConfiguration.workspace_id == WS_ID)
            ).scalar_one()
            assert config.client_secret_ref == "vault://oidc/acme/client_secret"
            assert config.client_id == "rotated-client"

    def test_group_role_map_and_claims_round_trip(self, client_factory):
        admin = client_factory(UserRole.ADMIN)
        payload = make_oidc_config_in(
            email_claim="mail",
            name_claim="displayName",
            groups_claim="memberOf",
            default_role="viewer",
            group_role_map={"forge-admins": "admin"},
            scopes=["openid", "email"],
        )
        response = admin.put(f"/workspaces/{WS_ID}/oidc", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["email_claim"] == "mail"
        assert body["name_claim"] == "displayName"
        assert body["groups_claim"] == "memberOf"
        assert body["default_role"] == "viewer"
        assert body["group_role_map"] == {"forge-admins": "admin"}
        assert body["scopes"] == ["openid", "email"]
