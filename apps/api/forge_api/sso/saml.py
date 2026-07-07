"""SAML 2.0 service-provider engine (F33): AuthnRequest build + Response validation.

Signature verification is delegated to ``signxml`` (a maintained XML-DSig
toolkit over lxml + cryptography) — never hand-rolled. Deviation from the
idealized slice doc: ``python3-saml``/``xmlsec1`` requires native
``libxmlsec1`` libraries which the foundation image does not carry; ``signxml``
provides the same enveloped-signature verification (exclusive C14N,
RSA-SHA256) as a pure-Python dependency.

XSW hardening: the assertion consumed downstream is parsed **only** from the
element set the verified signature actually covers (``VerifyResults.signed_xml``),
and any document carrying more than one ``saml:Assertion`` is rejected outright.
"""

from __future__ import annotations

import base64
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from cryptography.exceptions import InvalidSignature as CryptoInvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from lxml import etree
from signxml import XMLVerifier
from signxml.exceptions import InvalidInput, InvalidSignature

from forge_api.sso.errors import SamlValidationError
from forge_api.sso.saml_metadata import NS, parse_xml_hardened
from forge_contracts.sso import SamlAssertion, SamlIdpConfig

_STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
_SIG_ALG_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"


def _saml_instant(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SamlValidationError("malformed_timestamp", f"{field}={value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def new_request_id() -> str:
    """A fresh SAML message id (NCName: must not start with a digit)."""
    return f"_{uuid.uuid4().hex}"


class SamlSpService:
    """Implements the frozen :class:`forge_contracts.sso.SamlValidator` Protocol."""

    # -- SP-initiated login -------------------------------------------------- #

    def build_authn_request(
        self,
        config: SamlIdpConfig,
        *,
        sp_entity_id: str,
        acs_url: str,
        relay_state: str,
        sign: bool,
        sp_private_key_pem: str | None,
    ) -> tuple[str, str]:
        """Build a (optionally signed) HTTP-Redirect ``AuthnRequest``.

        Returns ``(redirect_url, request_id)``. With ``sign=True`` the query
        string carries ``SigAlg`` + ``Signature`` (RSA-SHA256 over the exact
        ``SAMLRequest=…&RelayState=…&SigAlg=…`` byte sequence — the
        HTTP-Redirect *DEFLATE* binding signs the query, not the XML).
        """
        request_id = new_request_id()
        samlp, saml = NS["samlp"], NS["saml"]
        root = etree.Element(f"{{{samlp}}}AuthnRequest", nsmap={"samlp": samlp, "saml": saml})
        root.set("ID", request_id)
        root.set("Version", "2.0")
        root.set("IssueInstant", _saml_instant(datetime.now(UTC)))
        root.set("Destination", config.sso_url)
        root.set("ProtocolBinding", "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST")
        root.set("AssertionConsumerServiceURL", acs_url)
        issuer = etree.SubElement(root, f"{{{saml}}}Issuer")
        issuer.text = sp_entity_id
        policy = etree.SubElement(root, f"{{{samlp}}}NameIDPolicy")
        policy.set("Format", config.name_id_format)
        policy.set("AllowCreate", "true")

        xml = etree.tostring(root)
        deflated = zlib.compress(xml, 9)[2:-4]  # raw DEFLATE (no zlib header/adler)
        saml_request = base64.b64encode(deflated).decode("ascii")

        params = [("SAMLRequest", saml_request)]
        if relay_state:
            params.append(("RelayState", relay_state))
        if sign:
            if not sp_private_key_pem:
                raise SamlValidationError(
                    "missing_sp_key", "sign_authn_requests=true but no SP private key"
                )
            params.append(("SigAlg", _SIG_ALG_RSA_SHA256))
            to_sign = urlencode(params).encode("ascii")
            key = load_pem_private_key(sp_private_key_pem.encode("utf-8"), password=None)
            # SigAlg is fixed to rsa-sha256, so the SP signing key must be RSA.
            if not isinstance(key, rsa.RSAPrivateKey):
                raise SamlValidationError(
                    "unsupported_sp_key", "SP signing key must be RSA (SigAlg=rsa-sha256)"
                )
            signature = key.sign(to_sign, padding.PKCS1v15(), hashes.SHA256())
            params.append(("Signature", base64.b64encode(signature).decode("ascii")))

        separator = "&" if "?" in config.sso_url else "?"
        return f"{config.sso_url}{separator}{urlencode(params)}", request_id

    # -- response validation ------------------------------------------------- #

    def validate_response(
        self,
        *,
        saml_response_b64: str,
        config: SamlIdpConfig,
        sp_entity_id: str,
        acs_url: str,
        want_signed: bool,
        expected_in_response_to: str | None,
        now: datetime,
        clock_skew_seconds: int,
    ) -> SamlAssertion:
        """Validate a POST-binding ``SAMLResponse`` and return its assertion.

        Order of checks: hardened parse → single-assertion shape → IdP status →
        XML-DSig signature (any configured rollover cert) → issuer → audience →
        conditions window (±skew, injected ``now``) → ``InResponseTo`` match.
        Raises :class:`SamlValidationError` with a stable reason code.
        """
        try:
            raw = base64.b64decode(saml_response_b64, validate=True)
        except Exception as exc:
            raise SamlValidationError("malformed_base64", str(exc)) from exc

        root = parse_xml_hardened(raw)
        if etree.QName(root).localname != "Response":
            raise SamlValidationError("not_a_response")

        assertions = root.findall(f".//{{{NS['saml']}}}Assertion")
        if len(assertions) != 1:
            raise SamlValidationError(
                "assertion_count", f"expected exactly 1 assertion, found {len(assertions)}"
            )

        self._check_status(root)

        assertion_el = self._verify_signature(root, config, want_signed=want_signed)
        return self._parse_assertion(
            assertion_el,
            config=config,
            sp_entity_id=sp_entity_id,
            expected_in_response_to=expected_in_response_to,
            now=now,
            clock_skew_seconds=clock_skew_seconds,
        )

    # -- internals ------------------------------------------------------------ #

    def _check_status(self, root: etree._Element) -> None:
        status = root.find(f"{{{NS['samlp']}}}Status/{{{NS['samlp']}}}StatusCode")
        if status is None or status.get("Value") != _STATUS_SUCCESS:
            value = status.get("Value") if status is not None else "missing"
            raise SamlValidationError("idp_status", value)

    def _verify_signature(
        self, root: etree._Element, config: SamlIdpConfig, *, want_signed: bool
    ) -> etree._Element:
        """Verify the XML-DSig signature and return the *covered* assertion.

        The assertion handed downstream is re-located inside
        ``VerifyResults.signed_xml`` — the exact element set the verified
        signature references — so a relocated/injected (XSW) assertion outside
        the signed subtree can never be consumed.
        """
        signatures = root.findall(".//{http://www.w3.org/2000/09/xmldsig#}Signature")
        if not signatures:
            raise SamlValidationError("unsigned", "response carries no XML signature")
        if len(signatures) > 1:
            raise SamlValidationError("multiple_signatures")

        last_error: Exception | None = None
        verified_xml: etree._Element | None = None
        for cert in config.x509_certs:
            try:
                result = XMLVerifier().verify(root, x509_cert=cert)
                # A single-signature document yields one VerifyResult; the union
                # return type also allows a list, so narrow before use.
                verified = result[-1] if isinstance(result, list) else result
                verified_xml = verified.signed_xml
                break
            except (InvalidSignature, InvalidInput, CryptoInvalidSignature, ValueError) as exc:
                last_error = exc
        if verified_xml is None:
            raise SamlValidationError(
                "bad_signature", str(last_error) if last_error else None
            ) from last_error

        if etree.QName(verified_xml).localname == "Assertion":
            return verified_xml
        covered = verified_xml.findall(f".//{{{NS['saml']}}}Assertion")
        if len(covered) != 1:
            raise SamlValidationError(
                "assertion_not_covered",
                "the verified signature does not cover exactly one assertion",
            )
        return covered[0]

    def _parse_assertion(
        self,
        assertion: etree._Element,
        *,
        config: SamlIdpConfig,
        sp_entity_id: str,
        expected_in_response_to: str | None,
        now: datetime,
        clock_skew_seconds: int,
    ) -> SamlAssertion:
        saml = NS["saml"]
        skew = timedelta(seconds=clock_skew_seconds)

        assertion_id = assertion.get("ID")
        if not assertion_id:
            raise SamlValidationError("missing_assertion_id")

        issuer_el = assertion.find(f"{{{saml}}}Issuer")
        issuer = issuer_el.text.strip() if issuer_el is not None and issuer_el.text else ""
        if issuer != config.entity_id:
            raise SamlValidationError("issuer_mismatch", issuer)

        name_id_el = assertion.find(f"{{{saml}}}Subject/{{{saml}}}NameID")
        if name_id_el is None or not (name_id_el.text or "").strip():
            raise SamlValidationError("missing_name_id")
        name_id = (name_id_el.text or "").strip()
        name_id_format = name_id_el.get("Format") or config.name_id_format

        # SubjectConfirmationData: InResponseTo + NotOnOrAfter (bearer).
        scd = assertion.find(
            f"{{{saml}}}Subject/{{{saml}}}SubjectConfirmation/{{{saml}}}SubjectConfirmationData"
        )
        in_response_to = scd.get("InResponseTo") if scd is not None else None

        # Conditions window (NotBefore / NotOnOrAfter with bounded skew).
        conditions = assertion.find(f"{{{saml}}}Conditions")
        if conditions is None:
            raise SamlValidationError("missing_conditions")
        not_before_raw = conditions.get("NotBefore")
        not_on_or_after_raw = conditions.get("NotOnOrAfter")
        if not not_on_or_after_raw:
            raise SamlValidationError("missing_conditions", "NotOnOrAfter is required")
        not_on_or_after = _parse_instant(not_on_or_after_raw, field="NotOnOrAfter")
        if now >= not_on_or_after + skew:
            raise SamlValidationError("expired", f"NotOnOrAfter={not_on_or_after_raw}")
        if not_before_raw:
            not_before = _parse_instant(not_before_raw, field="NotBefore")
            if now < not_before - skew:
                raise SamlValidationError("not_yet_valid", f"NotBefore={not_before_raw}")

        # AudienceRestriction must name this SP.
        audiences = [
            (el.text or "").strip()
            for el in conditions.findall(f"{{{saml}}}AudienceRestriction/{{{saml}}}Audience")
        ]
        if not audiences or sp_entity_id not in audiences:
            raise SamlValidationError("audience_mismatch", ", ".join(audiences) or "none")

        # InResponseTo must match the outstanding SP request when one is expected.
        if expected_in_response_to is not None and in_response_to != expected_in_response_to:
            raise SamlValidationError("in_response_to_mismatch", in_response_to or "none")

        authn_statement = assertion.find(f"{{{saml}}}AuthnStatement")
        session_index = authn_statement.get("SessionIndex") if authn_statement is not None else None

        attributes: dict[str, list[str]] = {}
        for attr in assertion.findall(f"{{{saml}}}AttributeStatement/{{{saml}}}Attribute"):
            attr_name = attr.get("Name")
            if not attr_name:
                continue
            values = [(v.text or "").strip() for v in attr.findall(f"{{{saml}}}AttributeValue")]
            attributes[attr_name] = [v for v in values if v]

        return SamlAssertion(
            assertion_id=assertion_id,
            name_id=name_id,
            name_id_format=name_id_format,
            session_index=session_index,
            issuer=issuer,
            attributes=attributes,
            not_on_or_after=not_on_or_after,
            in_response_to=in_response_to,
        )


def peek_in_response_to(saml_response_b64: str) -> str | None:
    """Pre-validation peek at the response-level ``InResponseTo``.

    Used only to look up the outstanding request in the replay guard; the value
    is re-verified against the *signed* assertion during full validation.
    """
    try:
        raw = base64.b64decode(saml_response_b64, validate=True)
    except Exception as exc:
        raise SamlValidationError("malformed_base64", str(exc)) from exc
    root = parse_xml_hardened(raw)
    return root.get("InResponseTo")


__all__ = ["SamlSpService", "new_request_id", "peek_in_response_to"]
