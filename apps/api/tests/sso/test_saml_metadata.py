"""Unit tests for XXE-hardened metadata parse + SP metadata render (AC1, AC11)."""

from __future__ import annotations

import pytest
from conftest import (
    ACS_URL,
    IDP_ENTITY_ID,
    IDP_SSO_URL,
    SP_ENTITY_ID,
    Keypair,
    cert_b64_body,
    make_idp_metadata,
)
from lxml import etree

from forge_api.sso.errors import SamlValidationError, SsoConfigError
from forge_api.sso.saml_metadata import (
    normalize_cert_pem,
    parse_idp_metadata,
    render_sp_metadata,
)


class TestParseIdpMetadata:
    def test_parse_extracts_all_fields(self, idp_keypair: Keypair):
        config = parse_idp_metadata(make_idp_metadata(idp_keypair))
        assert config.entity_id == IDP_ENTITY_ID
        assert config.sso_url == IDP_SSO_URL
        assert config.slo_url == "https://idp.acme-corp.example/slo"
        assert len(config.x509_certs) == 1
        assert "BEGIN CERTIFICATE" in config.x509_certs[0]
        assert config.name_id_format.endswith("emailAddress")

    def test_metadata_without_certs_rejected(self, idp_keypair: Keypair):
        xml = make_idp_metadata(idp_keypair).replace('use="signing"', 'use="encryption"')
        with pytest.raises(SsoConfigError):
            parse_idp_metadata(xml)

    def test_xxe_doctype_blocked(self, tmp_path):
        """A metadata doc that would exfiltrate a local file never resolves."""
        secret = tmp_path / "secret.txt"
        secret.write_text("exfiltrated-content")
        evil = (
            '<?xml version="1.0"?>\n'
            f'<!DOCTYPE md [<!ENTITY xxe SYSTEM "file://{secret}">]>\n'
            '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            'entityID="&xxe;"><md:IDPSSODescriptor/></md:EntityDescriptor>'
        )
        with pytest.raises(SamlValidationError) as err:
            parse_idp_metadata(evil)
        assert err.value.reason in ("dtd_forbidden", "malformed_xml")
        assert "exfiltrated-content" not in str(err.value)


class TestRenderSpMetadata:
    def test_sp_metadata_shape(self, idp_keypair: Keypair):
        sp = Keypair("sp-render")
        xml = render_sp_metadata(
            sp_entity_id=SP_ENTITY_ID,
            acs_url=ACS_URL,
            slo_url=SP_ENTITY_ID.replace("/metadata", "/slo"),
            sp_cert_pem=sp.cert_pem,
        )
        root = etree.fromstring(xml.encode())
        md = "{urn:oasis:names:tc:SAML:2.0:metadata}"
        assert root.get("entityID") == SP_ENTITY_ID
        acs = root.find(f"{md}SPSSODescriptor/{md}AssertionConsumerService")
        assert acs.get("Location") == ACS_URL
        assert acs.get("Binding") == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        slo = root.find(f"{md}SPSSODescriptor/{md}SingleLogoutService")
        assert slo.get("Location").endswith("/slo")
        assert cert_b64_body(sp.cert_pem) in xml
        # AC1: never the private key.
        assert "PRIVATE KEY" not in xml

    def test_round_trips_through_hardened_parser(self, idp_keypair: Keypair):
        sp = Keypair("sp-roundtrip")
        xml = render_sp_metadata(
            sp_entity_id=SP_ENTITY_ID,
            acs_url=ACS_URL,
            slo_url="https://x/slo",
            sp_cert_pem=sp.cert_pem,
        )
        from forge_api.sso.saml_metadata import parse_xml_hardened

        assert parse_xml_hardened(xml) is not None


class TestNormalizeCert:
    def test_accepts_raw_base64_and_pem(self, idp_keypair: Keypair):
        pem = idp_keypair.cert_pem
        assert normalize_cert_pem(pem) == normalize_cert_pem(cert_b64_body(pem))

    def test_rejects_garbage(self):
        with pytest.raises(SsoConfigError):
            normalize_cert_pem("not-base-64!!!")
