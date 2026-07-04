"""XXE-hardened SAML metadata parsing + SP metadata rendering (F33, pure).

Every XML document that enters the SSO layer is parsed through
:func:`parse_xml_hardened`: entity resolution off, network access off, DTD
loading off, and any document carrying a DOCTYPE is rejected outright — a
metadata/response fixture attempting to exfiltrate ``/etc/passwd`` via an
external entity never resolves (AC11).
"""

from __future__ import annotations

import base64
import textwrap

from lxml import etree

from forge_api.sso.errors import SamlValidationError, SsoConfigError
from forge_contracts.sso import SamlIdpConfig

NS = {
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
}

_BINDING_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
_BINDING_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"


def parse_xml_hardened(data: bytes | str) -> etree._Element:
    """Parse XML with entities/DTD/network disabled; reject any DOCTYPE."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SamlValidationError("malformed_xml", str(exc)) from exc
    docinfo = root.getroottree().docinfo
    if docinfo.doctype:
        raise SamlValidationError(
            "dtd_forbidden", "documents carrying a DOCTYPE/DTD are rejected (XXE hardening)"
        )
    return root


def normalize_cert_pem(cert: str) -> str:
    """Return a normalized PEM certificate from PEM or raw base64 DER input."""
    body = cert.replace("-----BEGIN CERTIFICATE-----", "")
    body = body.replace("-----END CERTIFICATE-----", "")
    body = "".join(body.split())
    if not body:
        raise SsoConfigError("empty X509 certificate")
    try:
        base64.b64decode(body, validate=True)
    except Exception as exc:
        raise SsoConfigError(f"invalid X509 certificate base64: {exc}") from exc
    wrapped = "\n".join(textwrap.wrap(body, 64))
    return f"-----BEGIN CERTIFICATE-----\n{wrapped}\n-----END CERTIFICATE-----\n"


def parse_idp_metadata(xml: bytes | str) -> SamlIdpConfig:
    """Parse IdP metadata XML into a :class:`SamlIdpConfig` (hardened parse)."""
    root = parse_xml_hardened(xml)
    if etree.QName(root).localname != "EntityDescriptor":
        raise SsoConfigError("metadata root must be an md:EntityDescriptor")
    entity_id = root.get("entityID")
    if not entity_id:
        raise SsoConfigError("metadata is missing entityID")
    idp = root.find("md:IDPSSODescriptor", NS)
    if idp is None:
        raise SsoConfigError("metadata has no IDPSSODescriptor")

    certs: list[str] = []
    for key_descriptor in idp.findall("md:KeyDescriptor", NS):
        use = key_descriptor.get("use")
        if use not in (None, "signing"):
            continue
        for cert_el in key_descriptor.findall(".//ds:X509Certificate", NS):
            if cert_el.text and cert_el.text.strip():
                pem = normalize_cert_pem(cert_el.text)
                if pem not in certs:
                    certs.append(pem)
    if not certs:
        raise SsoConfigError("metadata contains no signing certificates")

    sso_url: str | None = None
    for svc in idp.findall("md:SingleSignOnService", NS):
        if svc.get("Binding") == _BINDING_REDIRECT and svc.get("Location"):
            sso_url = svc.get("Location")
            break
        if sso_url is None and svc.get("Location"):
            sso_url = svc.get("Location")
    if not sso_url:
        raise SsoConfigError("metadata has no SingleSignOnService location")

    slo_url: str | None = None
    for svc in idp.findall("md:SingleLogoutService", NS):
        if svc.get("Location"):
            slo_url = svc.get("Location")
            break

    name_id_el = idp.find("md:NameIDFormat", NS)
    name_id_format = (
        name_id_el.text.strip()
        if name_id_el is not None and name_id_el.text
        else "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
    )
    return SamlIdpConfig(
        entity_id=entity_id,
        sso_url=sso_url,
        slo_url=slo_url,
        x509_certs=certs,
        name_id_format=name_id_format,
    )


def _cert_body(pem: str) -> str:
    body = pem.replace("-----BEGIN CERTIFICATE-----", "")
    body = body.replace("-----END CERTIFICATE-----", "")
    return "".join(body.split())


def render_sp_metadata(
    *,
    sp_entity_id: str,
    acs_url: str,
    slo_url: str,
    sp_cert_pem: str,
    want_assertions_signed: bool = True,
    authn_requests_signed: bool = True,
    name_id_format: str = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
) -> str:
    """Render SP metadata XML. Contains only *public* material (no private key)."""
    md = NS["md"]
    ds = NS["ds"]
    root = etree.Element(f"{{{md}}}EntityDescriptor", nsmap={"md": md, "ds": ds})
    root.set("entityID", sp_entity_id)
    spsso = etree.SubElement(root, f"{{{md}}}SPSSODescriptor")
    spsso.set("protocolSupportEnumeration", "urn:oasis:names:tc:SAML:2.0:protocol")
    spsso.set("AuthnRequestsSigned", "true" if authn_requests_signed else "false")
    spsso.set("WantAssertionsSigned", "true" if want_assertions_signed else "false")

    key_descriptor = etree.SubElement(spsso, f"{{{md}}}KeyDescriptor", use="signing")
    key_info = etree.SubElement(key_descriptor, f"{{{ds}}}KeyInfo")
    x509_data = etree.SubElement(key_info, f"{{{ds}}}X509Data")
    x509_cert = etree.SubElement(x509_data, f"{{{ds}}}X509Certificate")
    x509_cert.text = _cert_body(sp_cert_pem)

    slo = etree.SubElement(spsso, f"{{{md}}}SingleLogoutService")
    slo.set("Binding", _BINDING_POST)
    slo.set("Location", slo_url)

    nif = etree.SubElement(spsso, f"{{{md}}}NameIDFormat")
    nif.text = name_id_format

    acs = etree.SubElement(spsso, f"{{{md}}}AssertionConsumerService")
    acs.set("Binding", _BINDING_POST)
    acs.set("Location", acs_url)
    acs.set("index", "0")
    acs.set("isDefault", "true")

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


__all__ = [
    "NS",
    "normalize_cert_pem",
    "parse_idp_metadata",
    "parse_xml_hardened",
    "render_sp_metadata",
]
