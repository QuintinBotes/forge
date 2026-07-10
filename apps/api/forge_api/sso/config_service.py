"""SSO configuration + SCIM token lifecycle (F33 admin surface).

The SP signing keypair is generated here (RSA-2048 + self-signed cert via
``cryptography``); the private key is encrypted at rest with a dedicated
subkey of the instance master secret (the same key-separation scheme F37's
auth service uses) and is never serialized into :class:`SsoConfigOut`, logs,
or audit details.

Metadata fetching is a seam (``fetch_idp_metadata`` with an injectable httpx
transport + SSRF guard) so the suite runs fully offline against fixtures.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_api.auth.crypto import SecretCipher, default_cipher
from forge_api.auth.service import _resolve_master_key, _subkey
from forge_api.sso.errors import (
    DomainConflictError,
    LastAdminError,
    SsoConfigError,
)
from forge_api.sso.provisioning import count_local_active_admins, emit_sso_audit
from forge_api.sso.saml_metadata import normalize_cert_pem, parse_idp_metadata
from forge_contracts.sso import (
    AttributeMapping,
    OidcIdpConfig,
    SamlIdpConfig,
    SsoConfigIn,
    SsoConfigOut,
)
from forge_db.models import OidcConfiguration, ScimToken, SsoConfiguration, SsoDomain, Workspace
from forge_db.models.enums import SsoProtocol, UserRole

#: Cap for fetched IdP metadata documents (SSRF/resource guard).
MAX_METADATA_BYTES = 1_000_000


@lru_cache(maxsize=1)
def _sp_key_cipher() -> SecretCipher:
    """Process-wide cipher for SP private keys (own subkey of the master secret)."""
    master = _resolve_master_key(None)
    return default_cipher(_subkey(master, b"forge-sso-sp-key"))


@lru_cache(maxsize=1)
def _oidc_secret_cipher() -> SecretCipher:
    """Process-wide cipher for OIDC client secrets (own subkey — key separation)."""
    master = _resolve_master_key(None)
    return default_cipher(_subkey(master, b"forge-sso-oidc-secret"))


def generate_sp_keypair(common_name: str) -> tuple[str, str]:
    """Return ``(private_key_pem, cert_pem)`` — a fresh RSA-2048 SP signing pair."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64] or "forge-sp")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return key_pem, cert_pem


def fetch_idp_metadata(
    url: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 10.0
) -> str:
    """Fetch IdP metadata XML with an SSRF guard (HTTPS-only, no private hosts).

    ``transport`` is the offline seam: tests/workers inject an
    ``httpx.MockTransport`` serving fixtures; no live IdP is ever contacted in
    the suite.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SsoConfigError("metadata_url must use https")
    host = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local:
            raise SsoConfigError("metadata_url resolves to a private address")
    except ValueError:
        pass  # hostname, not a literal IP
    if host in ("localhost",):
        raise SsoConfigError("metadata_url must not target localhost")
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
        response = client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_METADATA_BYTES:
            raise SsoConfigError("metadata document exceeds the size cap")
        return response.text


class ScimTokenInfo(BaseModel):
    """Redacted SCIM-token view (never the raw token or its hash)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    token_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ScimTokenCreated(ScimTokenInfo):
    """Mint response — carries the plaintext ``token`` exactly once."""

    token: str


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SsoConfigService:
    """Admin-facing SSO config + SCIM token management on one session."""

    def __init__(
        self,
        session: Session,
        *,
        public_url: str,
        cipher: SecretCipher | None = None,
        metadata_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._session = session
        self._public_url = public_url.rstrip("/")
        self._cipher = cipher or _sp_key_cipher()
        self._metadata_transport = metadata_transport

    # -- URL derivation ------------------------------------------------------- #

    def _workspace(self, workspace_id: uuid.UUID) -> Workspace:
        workspace = self._session.get(Workspace, workspace_id)
        if workspace is None:
            raise SsoConfigError("workspace not found")
        return workspace

    def workspace_slug(self, workspace_id: uuid.UUID) -> str:
        return self._workspace(workspace_id).slug

    def sp_urls(self, slug: str) -> dict[str, str]:
        base = f"{self._public_url}/auth/saml/{slug}"
        return {
            "sp_entity_id": f"{base}/metadata",
            "sp_metadata_url": f"{base}/metadata",
            "sp_acs_url": f"{base}/acs",
            "sp_slo_url": f"{base}/slo",
        }

    # -- config CRUD ------------------------------------------------------------ #

    def get_config(self, workspace_id: uuid.UUID) -> SsoConfiguration | None:
        return self._session.execute(
            select(SsoConfiguration).where(SsoConfiguration.workspace_id == workspace_id)
        ).scalar_one_or_none()

    def get_config_by_slug(self, slug: str) -> tuple[Workspace, SsoConfiguration] | None:
        workspace = self._session.execute(
            select(Workspace).where(Workspace.slug == slug)
        ).scalar_one_or_none()
        if workspace is None:
            return None
        config = self.get_config(workspace.id)
        if config is None:
            return None
        return workspace, config

    def _resolve_idp(self, payload: SsoConfigIn) -> tuple[SamlIdpConfig, str | None]:
        if payload.idp is not None:
            idp = payload.idp.model_copy(
                update={"x509_certs": [normalize_cert_pem(c) for c in payload.idp.x509_certs]}
            )
            return idp, payload.metadata_url
        if payload.metadata_xml:
            return parse_idp_metadata(payload.metadata_xml), payload.metadata_url
        if payload.metadata_url:
            xml = fetch_idp_metadata(payload.metadata_url, transport=self._metadata_transport)
            return parse_idp_metadata(xml), payload.metadata_url
        raise SsoConfigError("one of idp, metadata_xml, or metadata_url is required")

    def put_config(
        self,
        workspace_id: uuid.UUID,
        payload: SsoConfigIn,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> SsoConfiguration:
        workspace = self._workspace(workspace_id)
        idp, metadata_url = self._resolve_idp(payload)
        urls = self.sp_urls(workspace.slug)

        config = self.get_config(workspace_id)
        if config is None:
            key_pem, cert_pem = generate_sp_keypair(f"forge-sp-{workspace.slug}")
            config = SsoConfiguration(
                workspace_id=workspace_id,
                protocol=SsoProtocol.SAML,
                sp_entity_id=urls["sp_entity_id"],
                sp_private_key_encrypted=self._cipher.encrypt(key_pem),
                sp_cert_pem=cert_pem,
                idp_entity_id=idp.entity_id,
                idp_sso_url=idp.sso_url,
                idp_x509_certs=idp.x509_certs,
            )
            self._session.add(config)

        config.enabled = payload.enabled
        config.metadata_url = metadata_url
        config.idp_entity_id = idp.entity_id
        config.idp_sso_url = idp.sso_url
        config.idp_slo_url = idp.slo_url
        config.idp_x509_certs = idp.x509_certs
        config.name_id_format = idp.name_id_format
        config.sp_entity_id = urls["sp_entity_id"]
        config.allow_idp_initiated = payload.allow_idp_initiated
        config.sign_authn_requests = payload.sign_authn_requests
        config.want_assertions_signed = payload.want_assertions_signed
        config.want_name_id_encrypted = payload.want_name_id_encrypted
        config.attribute_mapping = payload.attribute_mapping.model_dump()
        config.default_role = UserRole(payload.default_role)
        config.group_role_map = dict(payload.group_role_map)
        config.jit_provisioning = payload.jit_provisioning
        config.domains = sorted({d.strip().lower() for d in payload.domains if d.strip()})
        self._session.flush()
        self._project_domains(config)
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="sso.config_updated",
            actor_id=actor_id,
            target_type="sso_configuration",
            target_id=config.id,
            details={"enabled": config.enabled, "idp_entity_id": config.idp_entity_id},
        )
        return config

    def _project_domains(self, config: SsoConfiguration) -> None:
        wanted = set(config.domains)
        for domain in wanted:
            existing = self._session.execute(
                select(SsoDomain).where(SsoDomain.domain == domain)
            ).scalar_one_or_none()
            if existing is not None and existing.sso_configuration_id != config.id:
                raise DomainConflictError(domain)
        current = (
            self._session.execute(
                select(SsoDomain).where(SsoDomain.sso_configuration_id == config.id)
            )
            .scalars()
            .all()
        )
        for row in current:
            if row.domain not in wanted:
                self._session.delete(row)
        present = {row.domain for row in current}
        for domain in wanted - present:
            self._session.add(
                SsoDomain(
                    workspace_id=config.workspace_id,
                    domain=domain,
                    sso_configuration_id=config.id,
                    verified=True,  # V3: admin-asserted (DNS/email challenge is future)
                )
            )
        self._session.flush()

    def set_enabled(
        self,
        workspace_id: uuid.UUID,
        enabled: bool,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> SsoConfiguration:
        config = self.get_config(workspace_id)
        if config is None:
            raise SsoConfigError("no SSO configuration for workspace")
        if not enabled and count_local_active_admins(self._session, workspace_id) == 0:
            raise LastAdminError(
                "disabling SSO requires at least one active local break-glass admin"
            )
        config.enabled = enabled
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="sso.config_updated" if enabled else "sso.config_disabled",
            actor_id=actor_id,
            target_type="sso_configuration",
            target_id=config.id,
            details={"enabled": enabled},
        )
        return config

    def delete_config(self, workspace_id: uuid.UUID, *, actor_id: uuid.UUID | None = None) -> None:
        config = self.get_config(workspace_id)
        if config is None:
            raise SsoConfigError("no SSO configuration for workspace")
        for row in (
            self._session.execute(
                select(SsoDomain).where(SsoDomain.sso_configuration_id == config.id)
            )
            .scalars()
            .all()
        ):
            self._session.delete(row)
        config_id = config.id
        self._session.delete(config)
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="sso.config_disabled",
            actor_id=actor_id,
            target_type="sso_configuration",
            target_id=config_id,
            details={"deleted": True},
        )

    def decrypt_sp_key(self, config: SsoConfiguration) -> str:
        return self._cipher.decrypt(config.sp_private_key_encrypted)

    def to_out(self, config: SsoConfiguration) -> SsoConfigOut:
        workspace = self._workspace(config.workspace_id)
        urls = self.sp_urls(workspace.slug)
        return SsoConfigOut(
            id=str(config.id),
            workspace_id=str(config.workspace_id),
            protocol="saml",
            enabled=config.enabled,
            idp=SamlIdpConfig(
                entity_id=config.idp_entity_id,
                sso_url=config.idp_sso_url,
                slo_url=config.idp_slo_url,
                x509_certs=list(config.idp_x509_certs),
                name_id_format=config.name_id_format,
            ),
            sp_entity_id=config.sp_entity_id,
            sp_acs_url=urls["sp_acs_url"],
            sp_slo_url=urls["sp_slo_url"],
            sp_metadata_url=urls["sp_metadata_url"],
            sp_cert_pem=config.sp_cert_pem,
            domains=list(config.domains),
            allow_idp_initiated=config.allow_idp_initiated,
            sign_authn_requests=config.sign_authn_requests,
            want_assertions_signed=config.want_assertions_signed,
            attribute_mapping=AttributeMapping.model_validate(config.attribute_mapping or {}),
            default_role=config.default_role.value,
            group_role_map=dict(config.group_role_map or {}),
            jit_provisioning=config.jit_provisioning,
            last_metadata_refresh_at=config.last_metadata_refresh_at,
        )

    # -- OIDC config ------------------------------------------------------------- #

    @property
    def _oidc_cipher(self) -> SecretCipher:
        return _oidc_secret_cipher()

    def oidc_urls(self, slug: str) -> dict[str, str]:
        base = f"{self._public_url}/auth/oidc/{slug}"
        return {"redirect_uri": f"{base}/callback", "login_url": f"{base}/login"}

    def get_oidc_config(self, workspace_id: uuid.UUID) -> OidcConfiguration | None:
        return self._session.execute(
            select(OidcConfiguration).where(OidcConfiguration.workspace_id == workspace_id)
        ).scalar_one_or_none()

    def get_oidc_config_by_slug(self, slug: str) -> tuple[Workspace, OidcConfiguration] | None:
        workspace = self._session.execute(
            select(Workspace).where(Workspace.slug == slug)
        ).scalar_one_or_none()
        if workspace is None:
            return None
        config = self.get_oidc_config(workspace.id)
        if config is None:
            return None
        return workspace, config

    def put_oidc_config(
        self,
        workspace_id: uuid.UUID,
        payload: OidcIdpConfig,
        *,
        client_secret: str,
        enabled: bool = False,
        jit_provisioning: bool = True,
        domains: list[str] | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> OidcConfiguration:
        """Create/replace a workspace OIDC config; the client secret is encrypted."""
        self._workspace(workspace_id)
        config = self.get_oidc_config(workspace_id)
        if config is None:
            config = OidcConfiguration(
                workspace_id=workspace_id,
                issuer=payload.issuer,
                client_id=payload.client_id,
                client_secret_ref=payload.client_secret_ref,
                client_secret_encrypted=self._oidc_cipher.encrypt(client_secret),
            )
            self._session.add(config)
        else:
            config.client_secret_encrypted = self._oidc_cipher.encrypt(client_secret)
        config.enabled = enabled
        config.issuer = payload.issuer
        config.discovery_url = payload.discovery_url
        config.client_id = payload.client_id
        config.client_secret_ref = payload.client_secret_ref
        config.authorization_endpoint = payload.authorization_endpoint
        config.token_endpoint = payload.token_endpoint
        config.jwks_uri = payload.jwks_uri
        config.scopes = list(payload.scopes)
        config.email_claim = payload.email_claim
        config.name_claim = payload.name_claim
        config.groups_claim = payload.groups_claim
        config.default_role = UserRole(payload.default_role)
        config.group_role_map = dict(payload.group_role_map)
        config.jit_provisioning = jit_provisioning
        config.domains = sorted({d.strip().lower() for d in (domains or []) if d.strip()})
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="sso.config_updated",
            actor_id=actor_id,
            target_type="oidc_configuration",
            target_id=config.id,
            details={"enabled": config.enabled, "issuer": config.issuer, "protocol": "oidc"},
        )
        return config

    def decrypt_oidc_secret(self, config: OidcConfiguration) -> str:
        return self._oidc_cipher.decrypt(config.client_secret_encrypted)

    def oidc_config_dto(self, config: OidcConfiguration) -> OidcIdpConfig:
        """Rebuild the :class:`OidcIdpConfig` DTO from a stored row (no secret)."""
        return OidcIdpConfig(
            issuer=config.issuer,
            discovery_url=config.discovery_url,
            client_id=config.client_id,
            client_secret_ref=config.client_secret_ref,
            scopes=list(config.scopes or []),
            email_claim=config.email_claim,
            name_claim=config.name_claim,
            groups_claim=config.groups_claim,
            default_role=config.default_role.value,
            group_role_map=dict(config.group_role_map or {}),
            authorization_endpoint=config.authorization_endpoint,
            token_endpoint=config.token_endpoint,
            jwks_uri=config.jwks_uri,
        )

    # -- HRD ---------------------------------------------------------------------- #

    def discover(self, email: str) -> str | None:
        """Return the workspace slug owning ``email``'s domain, or ``None``."""
        domain = email.rsplit("@", 1)[-1].strip().lower()
        if not domain or "@" not in email:
            return None
        row = self._session.execute(
            select(SsoDomain, SsoConfiguration)
            .join(SsoConfiguration, SsoConfiguration.id == SsoDomain.sso_configuration_id)
            .where(SsoDomain.domain == domain, SsoConfiguration.enabled.is_(True))
        ).first()
        if row is None:
            return None
        _domain_row, config = row
        workspace = self._session.get(Workspace, config.workspace_id)
        return workspace.slug if workspace is not None else None

    # -- SCIM tokens ----------------------------------------------------------------- #

    def issue_scim_token(
        self,
        workspace_id: uuid.UUID,
        *,
        name: str,
        token_bytes: int = 32,
        expires_at: datetime | None = None,
        created_by: uuid.UUID | None = None,
    ) -> ScimTokenCreated:
        clash = self._session.execute(
            select(ScimToken).where(ScimToken.workspace_id == workspace_id, ScimToken.name == name)
        ).scalar_one_or_none()
        if clash is not None:
            raise SsoConfigError(f"a SCIM token named {name!r} already exists")
        raw = f"forge_scim_{secrets.token_urlsafe(token_bytes)}"
        row = ScimToken(
            workspace_id=workspace_id,
            name=name,
            token_hash=_hash_token(raw),
            token_prefix=raw[:8],
            created_by=created_by,
            expires_at=expires_at,
        )
        self._session.add(row)
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="scim.token_issued",
            actor_id=created_by,
            target_type="scim_token",
            target_id=row.id,
            details={"name": name, "token_prefix": row.token_prefix},
        )
        return ScimTokenCreated(
            id=row.id,
            name=row.name,
            token_prefix=row.token_prefix,
            created_at=row.created_at or datetime.now(UTC),
            expires_at=row.expires_at,
            token=raw,
        )

    def list_scim_tokens(self, workspace_id: uuid.UUID) -> list[ScimTokenInfo]:
        rows = (
            self._session.execute(
                select(ScimToken)
                .where(ScimToken.workspace_id == workspace_id)
                .order_by(ScimToken.created_at)
            )
            .scalars()
            .all()
        )
        return [
            ScimTokenInfo(
                id=row.id,
                name=row.name,
                token_prefix=row.token_prefix,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]

    def revoke_scim_token(
        self,
        workspace_id: uuid.UUID,
        token_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> bool:
        row = self._session.get(ScimToken, token_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        row.revoked_at = datetime.now(UTC)
        self._session.flush()
        emit_sso_audit(
            self._session,
            workspace_id=workspace_id,
            action="scim.token_revoked",
            actor_id=actor_id,
            target_type="scim_token",
            target_id=row.id,
            details={"name": row.name},
        )
        return True


def verify_scim_token(session: Session, raw_token: str) -> ScimToken | None:
    """Resolve a bearer token to its workspace-scoped record (constant-time).

    Returns ``None`` for unknown, revoked, or expired tokens; touches
    ``last_used_at`` on success.
    """
    candidate_hash = _hash_token(raw_token)
    rows = (
        session.execute(select(ScimToken).where(ScimToken.token_prefix == raw_token[:8]))
        .scalars()
        .all()
    )
    match: ScimToken | None = None
    for row in rows:
        if hmac.compare_digest(row.token_hash, candidate_hash):
            match = row
            break
    if match is None or match.revoked_at is not None:
        return None
    if match.expires_at is not None:
        expires = match.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return None
    match.last_used_at = datetime.now(UTC)
    session.flush()
    return match


__all__ = [
    "MAX_METADATA_BYTES",
    "ScimTokenCreated",
    "ScimTokenInfo",
    "SsoConfigService",
    "fetch_idp_metadata",
    "generate_sp_keypair",
    "verify_scim_token",
]
