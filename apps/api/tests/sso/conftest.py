"""Fixtures for the F33 enterprise-SSO suite (fully offline, no live IdP).

Provides ephemeral IdP/SP RSA keypairs (never real keys), a
``sign_saml_response`` builder that ``signxml``-signs responses so tests
control every validated field, an in-memory replay guard, a seeded SQLite
session factory (two workspaces + role users), and a TestClient factory with
dependency overrides — mirroring the F32 marketplace harness.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

os.environ.setdefault("FORGE_SECRET_KEY", "sso-suite-master-secret-0123456789")

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree
from signxml import XMLSigner, methods
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal, get_db
from forge_api.main import create_app
from forge_api.routers.saml import get_replay_guard
from forge_api.sso.config_service import SsoConfigService
from forge_api.sso.replay import InMemoryReplayGuard
from forge_api.sso.saml_metadata import NS
from forge_contracts import UserRole
from forge_contracts.sso import OidcConfigIn, SamlIdpConfig, SsoConfigIn
from forge_db.base import Base
from forge_db.models import User, Workspace

WS_ID = uuid.UUID("00000000-0000-0000-0000-00000000f0a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-00000000f0c3")
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-00000000f0b1")
MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-00000000f0b2")
OTHER_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-00000000f0b9")

PUBLIC_URL = "http://localhost:8000"
SP_ENTITY_ID = f"{PUBLIC_URL}/auth/saml/acme/metadata"
ACS_URL = f"{PUBLIC_URL}/auth/saml/acme/acs"
IDP_ENTITY_ID = "https://idp.acme-corp.example/saml"
IDP_SSO_URL = "https://idp.acme-corp.example/sso"

_ROLE_USER = {UserRole.ADMIN: ADMIN_ID, UserRole.MEMBER: MEMBER_ID}


class Keypair:
    """Ephemeral RSA-2048 keypair + self-signed cert (test-only, never real)."""

    def __init__(self, common_name: str) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = datetime.now(UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .sign(self.key, hashes.SHA256())
        )

    @property
    def key_pem(self) -> str:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    @property
    def cert_pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="session")
def idp_keypair() -> Keypair:
    return Keypair("idp-test")


@pytest.fixture(scope="session")
def wrong_keypair() -> Keypair:
    return Keypair("not-the-idp")


def _instant(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Frozen once at import so build_saml_response and validate_response share a
# single clock reference. The full suite runs 20+ minutes, so the default
# assertion-validity window below is deliberately WIDE (±1h around NOW): a
# narrow (±5min) window drifts out of range over that runtime and trips the
# real-clock validator, producing spurious "not_yet_valid" (early) or "expired"
# (late) failures. Tests that assert on the window itself pass explicit
# not_before / not_on_or_after values, so widening the default is safe.
NOW = datetime.now(UTC)


def build_saml_response(
    *,
    name_id: str = "dana@acme.com",
    idp_entity_id: str = IDP_ENTITY_ID,
    audience: str = SP_ENTITY_ID,
    acs_url: str = ACS_URL,
    in_response_to: str | None = None,
    assertion_id: str | None = None,
    attributes: dict[str, list[str]] | None = None,
    now: datetime | None = None,
    not_before: datetime | None = None,
    not_on_or_after: datetime | None = None,
) -> etree._Element:
    """Build an unsigned ``samlp:Response`` element with full control."""
    now = now or NOW
    not_before = not_before or (now - timedelta(hours=1))
    not_on_or_after = not_on_or_after or (now + timedelta(hours=1))
    assertion_id = assertion_id or f"_a{uuid.uuid4().hex}"
    samlp, saml = NS["samlp"], NS["saml"]

    root = etree.Element(f"{{{samlp}}}Response", nsmap={"samlp": samlp, "saml": saml})
    root.set("ID", f"_r{uuid.uuid4().hex}")
    root.set("Version", "2.0")
    root.set("IssueInstant", _instant(now))
    root.set("Destination", acs_url)
    if in_response_to:
        root.set("InResponseTo", in_response_to)
    issuer = etree.SubElement(root, f"{{{saml}}}Issuer")
    issuer.text = idp_entity_id
    status_el = etree.SubElement(root, f"{{{samlp}}}Status")
    code = etree.SubElement(status_el, f"{{{samlp}}}StatusCode")
    code.set("Value", "urn:oasis:names:tc:SAML:2.0:status:Success")

    assertion = etree.SubElement(root, f"{{{saml}}}Assertion")
    assertion.set("ID", assertion_id)
    assertion.set("Version", "2.0")
    assertion.set("IssueInstant", _instant(now))
    a_issuer = etree.SubElement(assertion, f"{{{saml}}}Issuer")
    a_issuer.text = idp_entity_id
    subject = etree.SubElement(assertion, f"{{{saml}}}Subject")
    nid = etree.SubElement(subject, f"{{{saml}}}NameID")
    nid.set("Format", "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress")
    nid.text = name_id
    confirmation = etree.SubElement(subject, f"{{{saml}}}SubjectConfirmation")
    confirmation.set("Method", "urn:oasis:names:tc:SAML:2.0:cm:bearer")
    scd = etree.SubElement(confirmation, f"{{{saml}}}SubjectConfirmationData")
    scd.set("NotOnOrAfter", _instant(not_on_or_after))
    scd.set("Recipient", acs_url)
    if in_response_to:
        scd.set("InResponseTo", in_response_to)
    conditions = etree.SubElement(assertion, f"{{{saml}}}Conditions")
    conditions.set("NotBefore", _instant(not_before))
    conditions.set("NotOnOrAfter", _instant(not_on_or_after))
    restriction = etree.SubElement(conditions, f"{{{saml}}}AudienceRestriction")
    aud = etree.SubElement(restriction, f"{{{saml}}}Audience")
    aud.text = audience
    authn = etree.SubElement(assertion, f"{{{saml}}}AuthnStatement")
    authn.set("AuthnInstant", _instant(now))
    authn.set("SessionIndex", f"_s{uuid.uuid4().hex[:12]}")
    if attributes:
        stmt = etree.SubElement(assertion, f"{{{saml}}}AttributeStatement")
        for attr_name, values in attributes.items():
            attr = etree.SubElement(stmt, f"{{{saml}}}Attribute")
            attr.set("Name", attr_name)
            for value in values:
                val = etree.SubElement(attr, f"{{{saml}}}AttributeValue")
                val.text = value
    return root


def sign_saml_response(
    response: etree._Element,
    keypair: Keypair,
    *,
    reference_uri: str | None = None,
) -> str:
    """Sign a response with the IdP test key; returns base64 for the POST binding.

    ``reference_uri`` narrows the signature to one element (used to build the
    XSW fixture, where only the assertion is signed).
    """
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    kwargs = {}
    if reference_uri is not None:
        kwargs["reference_uri"] = reference_uri
    signed = signer.sign(response, key=keypair.key_pem, cert=keypair.cert_pem, **kwargs)
    return base64.b64encode(etree.tostring(signed)).decode("ascii")


def encode_unsigned(response: etree._Element) -> str:
    return base64.b64encode(etree.tostring(response)).decode("ascii")


IDP_METADATA_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{entity_id}">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
        <ds:X509Data><ds:X509Certificate>{cert_b64}</ds:X509Certificate></ds:X509Data>
      </ds:KeyInfo>
    </md:KeyDescriptor>
    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>
    <md:SingleSignOnService
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
      Location="{sso_url}"/>
    <md:SingleLogoutService
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
      Location="{slo_url}"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
"""


def cert_b64_body(cert_pem: str) -> str:
    body = cert_pem.replace("-----BEGIN CERTIFICATE-----", "")
    body = body.replace("-----END CERTIFICATE-----", "")
    return "".join(body.split())


def make_idp_metadata(
    keypair: Keypair,
    *,
    entity_id: str = IDP_ENTITY_ID,
    sso_url: str = IDP_SSO_URL,
    slo_url: str = "https://idp.acme-corp.example/slo",
) -> str:
    return IDP_METADATA_TEMPLATE.format(
        entity_id=entity_id,
        cert_b64=cert_b64_body(keypair.cert_pem),
        sso_url=sso_url,
        slo_url=slo_url,
    )


# --------------------------------------------------------------------------- #
# DB + app fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.add(Workspace(id=OTHER_WS_ID, name="Globex", slug="globex"))
        session.flush()
        session.add(
            User(
                id=ADMIN_ID,
                workspace_id=WS_ID,
                email="admin@acme.test",
                name="Acme Admin",
                role=UserRole.ADMIN,
            )
        )
        session.add(
            User(
                id=MEMBER_ID,
                workspace_id=WS_ID,
                email="member@acme.test",
                name="Acme Member",
                role=UserRole.MEMBER,
            )
        )
        session.add(
            User(
                id=OTHER_ADMIN_ID,
                workspace_id=OTHER_WS_ID,
                email="admin@globex.test",
                name="Globex Admin",
                role=UserRole.ADMIN,
            )
        )
        session.commit()
    return factory


@pytest.fixture
def replay_guard() -> InMemoryReplayGuard:
    return InMemoryReplayGuard()


@pytest.fixture
def client_factory(
    session_factory: sessionmaker[Session], replay_guard: InMemoryReplayGuard
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def _build(
        role: UserRole = UserRole.ADMIN,
        *,
        workspace_id: uuid.UUID = WS_ID,
        authenticated: bool = True,
    ) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_replay_guard] = lambda: replay_guard
        if authenticated:
            user_id = _ROLE_USER.get(role, ADMIN_ID)
            if workspace_id == OTHER_WS_ID:
                user_id = OTHER_ADMIN_ID
            principal = Principal(
                user_id=user_id,
                workspace_id=workspace_id,
                role=role,
                email="sso-test@forge.local",
                auth_method="test",
                scopes=["*"],
            )
            app.dependency_overrides[get_current_principal] = lambda: principal
        client = TestClient(app, follow_redirects=False)
        clients.append(client)
        return client

    yield _build
    for client in clients:
        client.close()


def make_config_in(keypair: Keypair, **overrides) -> dict:
    """A valid manual-IdP ``SsoConfigIn`` payload as a JSON-able dict."""
    payload = SsoConfigIn(
        enabled=True,
        idp=SamlIdpConfig(
            entity_id=IDP_ENTITY_ID,
            sso_url=IDP_SSO_URL,
            x509_certs=[keypair.cert_pem],
        ),
        domains=["acme.com"],
        jit_provisioning=True,
    ).model_dump()
    payload.update(overrides)
    return payload


def make_oidc_config_in(**overrides) -> dict:
    """A valid ``OidcConfigIn`` admin-API payload as a JSON-able dict."""
    payload = OidcConfigIn(
        enabled=True,
        issuer="https://idp.oidc.example",
        client_id="forge-oidc-client",
        client_secret="s3cr3t-oidc-value",
        jit_provisioning=True,
    ).model_dump()
    payload.update(overrides)
    return payload


def install_config(
    session_factory: sessionmaker[Session],
    keypair: Keypair,
    *,
    workspace_id: uuid.UUID = WS_ID,
    **overrides,
):
    """Provision a SAML config directly through the service layer."""
    with session_factory() as session:
        service = SsoConfigService(session, public_url=PUBLIC_URL)
        config = service.put_config(
            workspace_id, SsoConfigIn.model_validate(make_config_in(keypair, **overrides))
        )
        session.commit()
        config_id = config.id
    with session_factory() as session:
        from forge_db.models import SsoConfiguration

        config = session.get(SsoConfiguration, config_id)
        session.expunge(config)
        return config


__all__ = [
    "ACS_URL",
    "ADMIN_ID",
    "IDP_ENTITY_ID",
    "IDP_SSO_URL",
    "MEMBER_ID",
    "OTHER_ADMIN_ID",
    "OTHER_WS_ID",
    "PUBLIC_URL",
    "SP_ENTITY_ID",
    "WS_ID",
    "Keypair",
    "build_saml_response",
    "cert_b64_body",
    "encode_unsigned",
    "install_config",
    "make_config_in",
    "make_idp_metadata",
    "make_oidc_config_in",
    "sign_saml_response",
]
