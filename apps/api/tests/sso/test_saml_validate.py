"""Unit tests for ``SamlSpService`` (AC2-AC8: signature, conditions, audience,
replay preconditions, IdP-initiated shape) — no HTTP, no DB, no network."""

from __future__ import annotations

import base64
import zlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import (
    ACS_URL,
    IDP_ENTITY_ID,
    IDP_SSO_URL,
    SP_ENTITY_ID,
    Keypair,
    build_saml_response,
    encode_unsigned,
    sign_saml_response,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree

from forge_api.sso.errors import SamlValidationError
from forge_api.sso.saml import SamlSpService
from forge_contracts.sso import SamlIdpConfig

NOW = datetime.now(UTC)
SKEW = 120


def _idp_config(keypair: Keypair, **overrides) -> SamlIdpConfig:
    defaults = {
        "entity_id": IDP_ENTITY_ID,
        "sso_url": IDP_SSO_URL,
        "x509_certs": [keypair.cert_pem],
    }
    defaults.update(overrides)
    return SamlIdpConfig(**defaults)


def _validate(b64: str, keypair: Keypair, *, expected_irt: str | None = None, now=None):
    return SamlSpService().validate_response(
        saml_response_b64=b64,
        config=_idp_config(keypair),
        sp_entity_id=SP_ENTITY_ID,
        acs_url=ACS_URL,
        want_signed=True,
        expected_in_response_to=expected_irt,
        now=now or NOW,
        clock_skew_seconds=SKEW,
    )


# -- AuthnRequest (AC2) -------------------------------------------------------- #


class TestBuildAuthnRequest:
    def test_signed_redirect_url(self, idp_keypair: Keypair):
        sp = Keypair("sp-test")
        url, request_id = SamlSpService().build_authn_request(
            _idp_config(idp_keypair),
            sp_entity_id=SP_ENTITY_ID,
            acs_url=ACS_URL,
            relay_state="/board",
            sign=True,
            sp_private_key_pem=sp.key_pem,
        )
        assert url.startswith(IDP_SSO_URL + "?")
        query = parse_qs(urlparse(url).query)
        assert query["RelayState"] == ["/board"]
        assert query["SigAlg"] == ["http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"]

        # SAMLRequest is deflated + base64: inflate and inspect the XML.
        inflated = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15)
        root = etree.fromstring(inflated)
        assert root.get("ID") == request_id
        assert root.get("AssertionConsumerServiceURL") == ACS_URL
        assert root.findtext("{urn:oasis:names:tc:SAML:2.0:assertion}Issuer") == SP_ENTITY_ID

        # The redirect-binding signature verifies with the SP public key over
        # the exact SAMLRequest=..&RelayState=..&SigAlg=.. byte sequence.
        raw_query = urlparse(url).query
        signed_part, _, sig_part = raw_query.rpartition("&Signature=")
        from urllib.parse import unquote

        signature = base64.b64decode(unquote(sig_part))
        sp.key.public_key().verify(
            signature, signed_part.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )

    def test_unsigned_request_has_no_signature_param(self, idp_keypair: Keypair):
        url, _ = SamlSpService().build_authn_request(
            _idp_config(idp_keypair),
            sp_entity_id=SP_ENTITY_ID,
            acs_url=ACS_URL,
            relay_state="",
            sign=False,
            sp_private_key_pem=None,
        )
        query = parse_qs(urlparse(url).query)
        assert "Signature" not in query and "SigAlg" not in query


# -- Response validation (AC3-AC8) ---------------------------------------------- #


class TestValidateResponse:
    def test_happy_path(self, idp_keypair: Keypair):
        b64 = sign_saml_response(
            build_saml_response(
                in_response_to="_req1",
                attributes={"groups": ["engineering", "forge-admins"]},
            ),
            idp_keypair,
        )
        assertion = _validate(b64, idp_keypair, expected_irt="_req1")
        assert assertion.name_id == "dana@acme.com"
        assert assertion.issuer == IDP_ENTITY_ID
        assert assertion.in_response_to == "_req1"
        assert assertion.attributes["groups"] == ["engineering", "forge-admins"]
        assert assertion.session_index

    def test_unsigned_rejected(self, idp_keypair: Keypair):
        b64 = encode_unsigned(build_saml_response())
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair)
        assert err.value.reason == "unsigned"

    def test_wrong_key_rejected(self, idp_keypair: Keypair, wrong_keypair: Keypair):
        b64 = sign_saml_response(build_saml_response(), wrong_keypair)
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair)
        assert err.value.reason == "bad_signature"

    def test_tampered_rejected(self, idp_keypair: Keypair):
        b64 = sign_saml_response(build_saml_response(), idp_keypair)
        tampered = base64.b64encode(
            base64.b64decode(b64).replace(b"dana@acme.com", b"evil@acme.com")
        ).decode()
        with pytest.raises(SamlValidationError) as err:
            _validate(tampered, idp_keypair)
        assert err.value.reason == "bad_signature"

    def test_expired_rejected(self, idp_keypair: Keypair):
        b64 = sign_saml_response(
            build_saml_response(not_on_or_after=NOW - timedelta(minutes=10)),
            idp_keypair,
        )
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair)
        assert err.value.reason == "expired"

    def test_not_before_future_rejected(self, idp_keypair: Keypair):
        b64 = sign_saml_response(
            build_saml_response(not_before=NOW + timedelta(minutes=10)),
            idp_keypair,
        )
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair)
        assert err.value.reason == "not_yet_valid"

    def test_within_skew_accepted(self, idp_keypair: Keypair):
        b64 = sign_saml_response(
            build_saml_response(not_on_or_after=NOW - timedelta(seconds=60)),
            idp_keypair,
        )
        assert _validate(b64, idp_keypair).name_id == "dana@acme.com"

    def test_audience_mismatch_rejected(self, idp_keypair: Keypair):
        b64 = sign_saml_response(
            build_saml_response(audience="https://some-other-sp.example/metadata"),
            idp_keypair,
        )
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair)
        assert err.value.reason == "audience_mismatch"

    def test_issuer_mismatch_rejected(self, idp_keypair: Keypair):
        b64 = sign_saml_response(
            build_saml_response(idp_entity_id="https://rogue-idp.example"),
            idp_keypair,
        )
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair)
        assert err.value.reason == "issuer_mismatch"

    def test_in_response_to_mismatch_rejected(self, idp_keypair: Keypair):
        b64 = sign_saml_response(build_saml_response(in_response_to="_other"), idp_keypair)
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair, expected_irt="_req1")
        assert err.value.reason == "in_response_to_mismatch"

    def test_idp_initiated_shape_accepted_when_unexpected_none(self, idp_keypair: Keypair):
        b64 = sign_saml_response(build_saml_response(), idp_keypair)
        assertion = _validate(b64, idp_keypair, expected_irt=None)
        assert assertion.in_response_to is None

    def test_cert_rollover_second_cert_validates(
        self, idp_keypair: Keypair, wrong_keypair: Keypair
    ):
        """A response signed by either configured cert validates (AC20 overlap)."""
        b64 = sign_saml_response(build_saml_response(), idp_keypair)
        config = _idp_config(
            wrong_keypair, x509_certs=[wrong_keypair.cert_pem, idp_keypair.cert_pem]
        )
        assertion = SamlSpService().validate_response(
            saml_response_b64=b64,
            config=config,
            sp_entity_id=SP_ENTITY_ID,
            acs_url=ACS_URL,
            want_signed=True,
            expected_in_response_to=None,
            now=NOW,
            clock_skew_seconds=SKEW,
        )
        assert assertion.name_id == "dana@acme.com"

    def test_xsw_extra_assertion_rejected(self, idp_keypair: Keypair):
        """XML Signature Wrapping: signature covers one assertion, a second
        (attacker) assertion is injected — the document is rejected."""
        assertion_id = "_axsw1"
        response = build_saml_response(assertion_id=assertion_id)
        b64 = sign_saml_response(response, idp_keypair, reference_uri=f"#{assertion_id}")
        root = etree.fromstring(base64.b64decode(b64))
        saml_ns = "{urn:oasis:names:tc:SAML:2.0:assertion}"
        evil = etree.fromstring(etree.tostring(root.find(f".//{saml_ns}Assertion")))
        evil.set("ID", "_evil")
        evil.find(f"{saml_ns}Subject/{saml_ns}NameID").text = "attacker@acme.com"
        root.insert(list(root).index(root.find(f".//{saml_ns}Assertion")), evil)
        doctored = base64.b64encode(etree.tostring(root)).decode()
        with pytest.raises(SamlValidationError) as err:
            _validate(doctored, idp_keypair)
        assert err.value.reason in ("assertion_count", "bad_signature")

    def test_xxe_doctype_rejected(self, idp_keypair: Keypair):
        """A response smuggling a DOCTYPE/external entity never resolves (AC11)."""
        evil = (
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
            b'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
            b"&xxe;</samlp:Response>"
        )
        with pytest.raises(SamlValidationError) as err:
            _validate(base64.b64encode(evil).decode(), idp_keypair)
        assert err.value.reason in ("dtd_forbidden", "malformed_xml")

    def test_failed_idp_status_rejected(self, idp_keypair: Keypair):
        response = build_saml_response()
        samlp = "{urn:oasis:names:tc:SAML:2.0:protocol}"
        response.find(f"{samlp}Status/{samlp}StatusCode").set(
            "Value", "urn:oasis:names:tc:SAML:2.0:status:AuthnFailed"
        )
        b64 = sign_saml_response(response, idp_keypair)
        with pytest.raises(SamlValidationError) as err:
            _validate(b64, idp_keypair)
        assert err.value.reason == "idp_status"
