"""SCIM 2.0 service-provider API tests (AC12-AC16, AC18, AC19)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    WS_ID,
    Keypair,
    build_saml_response,
    install_config,
    sign_saml_response,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from forge_api.auth.service import get_auth_service
from forge_contracts import UserRole
from forge_db.models import AuditLog, ScimToken, User


@pytest.fixture
def scim_token(client_factory) -> str:
    """A freshly-minted SCIM bearer token for the acme workspace."""
    admin = client_factory(UserRole.ADMIN)
    response = admin.post(f"/workspaces/{WS_ID}/scim/tokens", json={"name": "okta"})
    assert response.status_code == 201
    return response.json()["token"]


@pytest.fixture
def scim(client_factory, scim_token: str) -> TestClient:
    client = client_factory(authenticated=False)
    client.headers.update({"Authorization": f"Bearer {scim_token}"})
    return client


def _user_payload(email: str = "eve@acme.com", **overrides) -> dict:
    payload = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": email,
        "name": {"givenName": "Eve", "familyName": "Vance"},
        "emails": [{"value": email, "primary": True}],
        "externalId": f"okta|{email}",
        "active": True,
    }
    payload.update(overrides)
    return payload


class TestScimAuth:
    def test_no_token_401_scim_error_body(self, client_factory):
        anon = client_factory(authenticated=False)
        response = anon.get("/scim/v2/Users")
        assert response.status_code == 401
        body = response.json()
        assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert body["status"] == "401"
        assert response.headers["content-type"].startswith("application/scim+json")

    def test_garbage_token_401(self, client_factory):
        anon = client_factory(authenticated=False)
        anon.headers.update({"Authorization": "Bearer forge_scim_not-a-real-token"})
        assert anon.get("/scim/v2/Users").status_code == 401

    def test_revoked_token_401(self, client_factory, scim_token, session_factory):
        with session_factory() as session:
            row = session.execute(select(ScimToken)).scalar_one()
            row.revoked_at = datetime.now(UTC)
            session.commit()
        client = client_factory(authenticated=False)
        client.headers.update({"Authorization": f"Bearer {scim_token}"})
        assert client.get("/scim/v2/Users").status_code == 401

    def test_expired_token_401(self, client_factory, scim_token, session_factory):
        with session_factory() as session:
            row = session.execute(select(ScimToken)).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            session.commit()
        client = client_factory(authenticated=False)
        client.headers.update({"Authorization": f"Bearer {scim_token}"})
        assert client.get("/scim/v2/Users").status_code == 401

    def test_valid_token_resolves_workspace_and_touches_last_used(self, scim, session_factory):
        assert scim.get("/scim/v2/Users").status_code == 200
        with session_factory() as session:
            row = session.execute(select(ScimToken)).scalar_one()
            assert row.workspace_id == WS_ID
            assert row.last_used_at is not None

    def test_discovery_docs_need_token_too(self, client_factory, scim):
        anon = client_factory(authenticated=False)
        for path in ("/scim/v2/ServiceProviderConfig", "/scim/v2/ResourceTypes"):
            assert anon.get(path).status_code == 401
            assert scim.get(path).status_code == 200
        spc = scim.get("/scim/v2/ServiceProviderConfig").json()
        assert spc["patch"]["supported"] is True
        assert spc["bulk"]["supported"] is False


class TestScimUsers:
    def test_create_read_list_filter(self, scim, session_factory):
        created = scim.post("/scim/v2/Users", json=_user_payload())
        assert created.status_code == 201, created.text
        assert created.headers["content-type"].startswith("application/scim+json")
        body = created.json()
        scim_id = body["id"]
        assert body["userName"] == "eve@acme.com"
        assert body["meta"]["location"].endswith(f"/scim/v2/Users/{scim_id}")
        assert created.headers["location"].endswith(f"/scim/v2/Users/{scim_id}")

        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "eve@acme.com")).scalar_one()
            assert user.external_managed is True
            assert user.workspace_id == WS_ID

        fetched = scim.get(f"/scim/v2/Users/{scim_id}")
        assert fetched.status_code == 200
        assert fetched.json()["externalId"] == "okta|eve@acme.com"

        listed = scim.get("/scim/v2/Users", params={"filter": 'userName eq "eve@acme.com"'})
        assert listed.status_code == 200
        assert listed.json()["totalResults"] == 1
        assert listed.json()["Resources"][0]["id"] == scim_id

        empty = scim.get("/scim/v2/Users", params={"filter": 'userName eq "nobody@acme.com"'})
        assert empty.json()["totalResults"] == 0

    def test_invalid_filter_400(self, scim):
        response = scim.get("/scim/v2/Users", params={"filter": 'title gt "x"'})
        assert response.status_code == 400
        assert response.json()["scimType"] == "invalidFilter"

    def test_pagination_deterministic(self, scim):
        for i in range(5):
            assert (
                scim.post("/scim/v2/Users", json=_user_payload(f"user{i}@acme.com")).status_code
                == 201
            )
        page1 = scim.get("/scim/v2/Users", params={"startIndex": 1, "count": 2}).json()
        page2 = scim.get("/scim/v2/Users", params={"startIndex": 3, "count": 2}).json()
        assert page1["totalResults"] == 5 and page2["totalResults"] == 5
        assert page1["itemsPerPage"] == 2 and page2["itemsPerPage"] == 2
        ids1 = [r["id"] for r in page1["Resources"]]
        ids2 = [r["id"] for r in page2["Resources"]]
        assert not set(ids1) & set(ids2)
        again = scim.get("/scim/v2/Users", params={"startIndex": 1, "count": 2}).json()
        assert [r["id"] for r in again["Resources"]] == ids1

    def test_uniqueness_conflict_409(self, scim):
        assert scim.post("/scim/v2/Users", json=_user_payload()).status_code == 201
        dup = scim.post("/scim/v2/Users", json=_user_payload())
        assert dup.status_code == 409
        assert dup.json()["scimType"] == "uniqueness"

    def test_unknown_user_404(self, scim):
        assert scim.get("/scim/v2/Users/deadbeef").status_code == 404

    def test_put_replace_updates_fields(self, scim):
        scim_id = scim.post("/scim/v2/Users", json=_user_payload()).json()["id"]
        replaced = scim.put(
            f"/scim/v2/Users/{scim_id}",
            json=_user_payload(displayName="Eve V.", externalId="okta|new-id"),
        )
        assert replaced.status_code == 200
        assert replaced.json()["displayName"] == "Eve V."
        assert replaced.json()["externalId"] == "okta|new-id"

    def test_patch_active_false_deprovisions(self, scim, session_factory):
        """AC15: PATCH active=false revokes sessions/tokens + blocks SAML."""
        scim_id = scim.post("/scim/v2/Users", json=_user_payload()).json()["id"]
        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "eve@acme.com")).scalar_one()
            user_id = user.id

        # Give eve a live Forge session/agent token (the F37 in-memory store).
        _info, token = get_auth_service().bootstrap_key(
            workspace_id=WS_ID, name="eve-session", role=UserRole.MEMBER, user_id=user_id
        )
        assert get_auth_service().authenticate(token)

        patched = scim.patch(
            f"/scim/v2/Users/{scim_id}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
        )
        assert patched.status_code == 200
        assert patched.json()["active"] is False

        with session_factory() as session:
            user = session.get(User, user_id)
            assert user.is_active is False
            assert user.deactivated_at is not None

        # Sessions revoked immediately: the minted key no longer authenticates.
        from forge_api.auth.service import AuthenticationError

        with pytest.raises(AuthenticationError):
            get_auth_service().authenticate(token)

    def test_deactivated_user_blocked_from_saml_login(
        self, scim, client_factory, session_factory, idp_keypair: Keypair
    ):
        install_config(session_factory, idp_keypair, allow_idp_initiated=True)
        scim_id = scim.post("/scim/v2/Users", json=_user_payload()).json()["id"]
        assert scim.delete(f"/scim/v2/Users/{scim_id}").status_code == 204

        saml_client = client_factory(authenticated=False)
        b64 = sign_saml_response(build_saml_response(name_id="eve@acme.com"), idp_keypair)
        response = saml_client.post("/auth/saml/acme/acs", data={"SAMLResponse": b64})
        assert response.status_code == 403

    def test_patch_no_path_object_value(self, scim):
        """Entra ID style: PATCH with a bare object value and no path."""
        scim_id = scim.post("/scim/v2/Users", json=_user_payload()).json()["id"]
        patched = scim.patch(
            f"/scim/v2/Users/{scim_id}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "Replace", "value": {"active": False}}],
            },
        )
        assert patched.status_code == 200
        assert patched.json()["active"] is False

    def test_reactivate_clears_deactivated_at(self, scim, session_factory):
        scim_id = scim.post("/scim/v2/Users", json=_user_payload()).json()["id"]
        scim.patch(
            f"/scim/v2/Users/{scim_id}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
        )
        reactivated = scim.patch(
            f"/scim/v2/Users/{scim_id}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": True}],
            },
        )
        assert reactivated.json()["active"] is True
        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "eve@acme.com")).scalar_one()
            assert user.is_active is True
            assert user.deactivated_at is None


class TestScimGroups:
    def test_group_membership_drives_effective_role(self, scim, session_factory, idp_keypair):
        """AC16: group → role mapping; removal reverts to default_role."""
        install_config(
            session_factory,
            idp_keypair,
            group_role_map={"forge-viewers": "viewer", "forge-admins": "admin"},
        )
        user_id = scim.post("/scim/v2/Users", json=_user_payload()).json()["id"]

        group = scim.post(
            "/scim/v2/Groups",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "forge-viewers",
                "members": [],
            },
        )
        assert group.status_code == 201
        group_id = group.json()["id"]

        added = scim.patch(
            f"/scim/v2/Groups/{group_id}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}],
            },
        )
        assert added.status_code == 200
        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "eve@acme.com")).scalar_one()
            assert user.role == UserRole.VIEWER

        # Highest privilege across memberships wins.
        admins = scim.post(
            "/scim/v2/Groups",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "forge-admins",
                "members": [{"value": user_id}],
            },
        )
        assert admins.status_code == 201
        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "eve@acme.com")).scalar_one()
            assert user.role == UserRole.ADMIN

        # Removing the admin membership drops back to viewer; removing all
        # memberships reverts to the config default_role (member).
        removed = scim.patch(
            f"/scim/v2/Groups/{admins.json()['id']}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": f'members[value eq "{user_id}"]',
                    }
                ],
            },
        )
        assert removed.status_code == 200
        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "eve@acme.com")).scalar_one()
            assert user.role == UserRole.VIEWER

        assert scim.delete(f"/scim/v2/Groups/{group_id}").status_code == 204
        with session_factory() as session:
            user = session.execute(select(User).where(User.email == "eve@acme.com")).scalar_one()
            assert user.role == UserRole.MEMBER

    def test_group_crud(self, scim):
        created = scim.post(
            "/scim/v2/Groups",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "engineering",
            },
        )
        assert created.status_code == 201
        gid = created.json()["id"]
        assert scim.get(f"/scim/v2/Groups/{gid}").json()["displayName"] == "engineering"
        listed = scim.get("/scim/v2/Groups").json()
        assert listed["totalResults"] == 1
        dup = scim.post(
            "/scim/v2/Groups",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "engineering",
            },
        )
        assert dup.status_code == 409


class TestAuditCompleteness:
    def test_scim_actions_audited_and_redacted(self, scim, scim_token, session_factory):
        """AC19: create/update/deactivate each emit exactly one audit event;
        AC18: no raw token ever lands in audit details."""
        scim_id = scim.post("/scim/v2/Users", json=_user_payload()).json()["id"]
        scim.put(f"/scim/v2/Users/{scim_id}", json=_user_payload(displayName="E"))
        scim.delete(f"/scim/v2/Users/{scim_id}")
        with session_factory() as session:
            rows = (
                session.execute(select(AuditLog).where(AuditLog.workspace_id == WS_ID))
                .scalars()
                .all()
            )
            actions = [row.action for row in rows]
            assert actions.count("scim.user_created") == 1
            assert actions.count("scim.user_updated") == 1
            assert actions.count("sso.user_deprovisioned") == 1
            blob = " ".join(str(row.details) for row in rows)
            assert scim_token not in blob
