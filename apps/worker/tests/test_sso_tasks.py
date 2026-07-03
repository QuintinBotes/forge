"""F33 SSO worker task tests (AC20 + replay cleanup + deprovision fan-out).

Deterministic cores over in-memory SQLite; the IdP metadata "fetch" goes
through an ``httpx.MockTransport`` serving fixtures — no live IdP, no network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import sessionmaker

from forge_api.sso.config_service import SsoConfigService
from forge_api.sso.replay import DbReplayGuard
from forge_contracts.sso import SamlIdpConfig, SsoConfigIn
from forge_db.base import Base
from forge_db.models import AuditLog, SamlReplay, SsoConfiguration, Workspace
from forge_worker.tasks.sso import (
    cleanup_saml_replay_core,
    propagate_deprovision_core,
    refresh_all_saml_metadata_core,
    refresh_saml_metadata_core,
)

WS_ID = uuid.UUID("00000000-0000-0000-0000-00000000e0a1")
METADATA_URL = "https://idp.acme-corp.example/metadata.xml"


def _cert_pem(cn: str) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _metadata_xml(cert_pem: str) -> str:
    body = "".join(
        cert_pem.replace("-----BEGIN CERTIFICATE-----", "")
        .replace("-----END CERTIFICATE-----", "")
        .split()
    )
    return f"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    entityID="https://idp.acme-corp.example/saml">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
        <ds:X509Data><ds:X509Certificate>{body}</ds:X509Certificate></ds:X509Data>
      </ds:KeyInfo>
    </md:KeyDescriptor>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="https://idp.acme-corp.example/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
"""


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        session.commit()
    return factory


@pytest.fixture
def config_id(session_factory) -> uuid.UUID:
    cert = _cert_pem("idp-original")
    with session_factory() as session:
        service = SsoConfigService(session, public_url="http://localhost:8000")
        config = service.put_config(
            WS_ID,
            SsoConfigIn(
                enabled=True,
                metadata_url=METADATA_URL,
                idp=SamlIdpConfig(
                    entity_id="https://idp.acme-corp.example/saml",
                    sso_url="https://idp.acme-corp.example/sso",
                    x509_certs=[cert],
                ),
            ),
        )
        session.commit()
        return config.id


def _transport_serving(xml: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == METADATA_URL
        return httpx.Response(200, text=xml)

    return httpx.MockTransport(handler)


class TestMetadataRefresh:
    def test_refresh_appends_rotated_cert_and_keeps_old(
        self, session_factory, config_id
    ):
        """AC20: a rotated IdP cert is appended; the prior cert stays."""
        rotated = _cert_pem("idp-rotated")
        report = refresh_saml_metadata_core(
            session_factory, config_id, transport=_transport_serving(_metadata_xml(rotated))
        )
        assert report["status"] == "refreshed"
        assert report["certs_added"] == 1
        assert report["certs_total"] == 2
        with session_factory() as session:
            config = session.get(SsoConfiguration, config_id)
            assert len(config.idp_x509_certs) == 2
            assert config.last_metadata_refresh_at is not None

    def test_refresh_is_idempotent(self, session_factory, config_id):
        rotated = _cert_pem("idp-rotated")
        transport = _transport_serving(_metadata_xml(rotated))
        refresh_saml_metadata_core(session_factory, config_id, transport=transport)
        report = refresh_saml_metadata_core(
            session_factory, config_id, transport=transport
        )
        assert report["certs_added"] == 0
        assert report["certs_total"] == 2

    def test_refresh_all_covers_every_config_with_url(
        self, session_factory, config_id
    ):
        rotated = _cert_pem("idp-rotated")
        count = refresh_all_saml_metadata_core(
            session_factory, transport=_transport_serving(_metadata_xml(rotated))
        )
        assert count == 1

    def test_missing_config_reports_missing(self, session_factory):
        report = refresh_saml_metadata_core(
            session_factory, uuid.uuid4(), transport=_transport_serving("")
        )
        assert report["status"] == "missing"


class TestReplayCleanup:
    def test_evicts_only_expired_rows(self, session_factory):
        now = datetime.now(UTC)
        with session_factory() as session:
            session.add(
                SamlReplay(replay_id="assertion:_old", expires_at=now - timedelta(hours=1))
            )
            session.add(
                SamlReplay(replay_id="assertion:_live", expires_at=now + timedelta(hours=1))
            )
            session.commit()
        assert cleanup_saml_replay_core(session_factory) == 1
        with session_factory() as session:
            remaining = [
                rid for (rid,) in session.execute(select(SamlReplay.replay_id)).all()
            ]
            assert remaining == ["assertion:_live"]

    def test_db_replay_guard_semantics(self, session_factory):
        with session_factory() as session:
            guard = DbReplayGuard(session)
            guard.register_request("_r1", ttl_seconds=600)
            assert guard.consume_request("_r1") is True
            assert guard.consume_request("_r1") is False  # one-shot
            assert guard.seen_assertion("_a1", ttl_seconds=600) is False
            assert guard.seen_assertion("_a1", ttl_seconds=600) is True  # replay


class TestPropagateDeprovision:
    def test_emits_audit_and_revokes(self, session_factory):
        calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        user_id = uuid.uuid4()

        def revoker(ws, uid):
            calls.append((ws, uid))
            return 2

        report = propagate_deprovision_core(
            session_factory, WS_ID, user_id, revoke_sessions=revoker
        )
        assert report == {"status": "propagated", "revoked": 2}
        assert calls == [(WS_ID, user_id)]
        with session_factory() as session:
            row = session.execute(
                select(AuditLog).where(AuditLog.action == "sso.deprovision_propagated")
            ).scalar_one()
            assert row.target_id == user_id


class TestTaskRegistration:
    def test_celery_tasks_registered(self):
        from forge_worker.celery_app import celery_app

        for name in (
            "sso.refresh_saml_metadata",
            "sso.refresh_all_saml_metadata",
            "sso.cleanup_saml_replay",
            "sso.propagate_deprovision",
        ):
            assert name in celery_app.tasks

    def test_beat_entries_present(self):
        from forge_worker.beat import BEAT_SCHEDULE

        assert BEAT_SCHEDULE["sso-refresh-saml-metadata"]["task"] == (
            "sso.refresh_all_saml_metadata"
        )
        assert BEAT_SCHEDULE["sso-cleanup-saml-replay"]["task"] == (
            "sso.cleanup_saml_replay"
        )


def test_metadata_fetch_requires_https():
    from forge_api.sso.config_service import fetch_idp_metadata
    from forge_api.sso.errors import SsoConfigError

    with pytest.raises(SsoConfigError):
        fetch_idp_metadata("http://idp.example/metadata.xml")
    with pytest.raises(SsoConfigError):
        fetch_idp_metadata("https://127.0.0.1/metadata.xml")
    with pytest.raises(SsoConfigError):
        fetch_idp_metadata("https://localhost/metadata.xml")
