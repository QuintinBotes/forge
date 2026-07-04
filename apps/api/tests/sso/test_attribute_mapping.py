"""Unit tests for attribute → identity mapping and role resolution (AC9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_api.sso.attribute_mapping import map_assertion, resolve_role
from forge_contracts.sso import AttributeMapping, SamlAssertion


def _assertion(attributes: dict[str, list[str]] | None = None) -> SamlAssertion:
    return SamlAssertion(
        assertion_id="_a1",
        name_id="Dana@Acme.com",
        name_id_format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        issuer="https://idp.example",
        attributes=attributes or {},
        not_on_or_after=datetime.now(UTC) + timedelta(minutes=5),
    )


class TestResolveRole:
    def test_highest_privilege_wins(self):
        role_map = {"forge-admins": "admin", "forge-eng": "member"}
        assert resolve_role(["forge-eng", "forge-admins"], role_map, "viewer") == "admin"

    def test_defaults_when_no_mapping_matches(self):
        assert resolve_role(["random-group"], {"forge-admins": "admin"}, "member") == "member"

    def test_rank_tie_broken_deterministically(self):
        # agent-runner and viewer share rank 0: member > agent-runner > viewer.
        role_map = {"g1": "viewer", "g2": "agent-runner"}
        assert resolve_role(["g1", "g2"], role_map, "member") == "agent-runner"

    def test_bad_role_value_in_map_is_ignored(self):
        assert resolve_role(["g"], {"g": "superuser"}, "member") == "member"


class TestMapAssertion:
    def test_no_implicit_admin_from_attributes(self):
        """An IdP attribute literally claiming role=admin grants nothing (AC9)."""
        identity = map_assertion(
            _assertion({"role": ["admin"], "groups": ["unmapped"]}),
            mapping=AttributeMapping(groups="groups"),
            group_role_map={},
            default_role="member",
        )
        assert identity.role == "member"

    def test_group_map_grants_admin(self):
        identity = map_assertion(
            _assertion({"groups": ["forge-admins"]}),
            mapping=AttributeMapping(groups="groups"),
            group_role_map={"forge-admins": "admin"},
            default_role="member",
        )
        assert identity.role == "admin"
        assert identity.groups == ["forge-admins"]

    def test_email_from_name_id_when_unmapped(self):
        identity = map_assertion(
            _assertion(),
            mapping=AttributeMapping(),
            group_role_map={},
            default_role="member",
        )
        assert identity.email == "dana@acme.com"  # lower-cased NameID
        assert identity.external_id == "Dana@Acme.com"

    def test_email_from_mapped_attribute(self):
        identity = map_assertion(
            _assertion({"mail": ["dana.w@acme.com"]}),
            mapping=AttributeMapping(email="mail"),
            group_role_map={},
            default_role="member",
        )
        assert identity.email == "dana.w@acme.com"

    def test_name_from_first_and_last(self):
        first = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"
        last = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname"
        identity = map_assertion(
            _assertion({first: ["Dana"], last: ["Whitcombe"]}),
            mapping=AttributeMapping(),
            group_role_map={},
            default_role="member",
        )
        assert identity.name == "Dana Whitcombe"
