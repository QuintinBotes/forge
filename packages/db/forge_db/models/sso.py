"""F33 enterprise SSO models: SAML config, HRD domains, SCIM tokens/groups.

All tables are workspace-scoped (tenant isolation). The SP signing private key
is stored encrypted (``sp_private_key_encrypted``) and never leaves the API
layer; ``idp_x509_certs`` holds *public* IdP signing certificates (a list, so
certificate rollover keeps old + new during the overlap window).

``saml_replay`` is the Postgres fallback replay store for deployments without
Redis; rows are evicted by the worker's ``cleanup_saml_replay`` beat task.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import ForgeModel, WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import ExternalIdentityProvider, SsoProtocol, UserRole


class SsoConfiguration(WorkspaceScopedModel):
    """One SAML configuration per workspace (V3: single IdP per tenant)."""

    __tablename__ = "sso_configuration"
    __table_args__ = (UniqueConstraint("workspace_id"),)

    protocol: Mapped[SsoProtocol] = mapped_column(
        enum_type(SsoProtocol), default=SsoProtocol.SAML, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    idp_entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    idp_sso_url: Mapped[str] = mapped_column(Text, nullable=False)
    idp_slo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: list[str] of PEM signing certs (first = primary, rest = rollover).
    idp_x509_certs: Mapped[list[str]] = mapped_column(json_type(), nullable=False)
    name_id_format: Mapped[str] = mapped_column(
        Text,
        default="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        nullable=False,
    )
    sp_entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: SP signing key, encrypted at rest (Fernet under FORGE_SECRET_KEY subkey).
    sp_private_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sp_cert_pem: Mapped[str] = mapped_column(Text, nullable=False)
    allow_idp_initiated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sign_authn_requests: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    want_assertions_signed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    want_name_id_encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attribute_mapping: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    default_role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole), default=UserRole.MEMBER, nullable=False
    )
    group_role_map: Mapped[dict[str, Any]] = mapped_column(
        json_type(), default=dict, nullable=False
    )
    jit_provisioning: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Editable input; projected into ``sso_domain`` rows for DB-enforced HRD.
    domains: Mapped[list[str]] = mapped_column(json_type(), default=list, nullable=False)
    last_metadata_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - trivial, secret-safe
        return (
            f"SsoConfiguration(id={self.id!r}, workspace_id={self.workspace_id!r}, "
            f"idp_entity_id={self.idp_entity_id!r}, sp_private_key=<redacted>)"
        )


class SsoDomain(WorkspaceScopedModel):
    """Home-realm-discovery row: a domain routes to exactly one IdP globally."""

    __tablename__ = "sso_domain"
    __table_args__ = (UniqueConstraint("domain", name="uq_sso_domain_domain"),)

    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    sso_configuration_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sso_configuration.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ExternalIdentity(WorkspaceScopedModel):
    """Links a Forge ``app_user`` to an IdP subject (SAML NameID / SCIM id).

    Complements — never replaces — the V1 ``app_user.auth_provider`` /
    ``auth_subject`` social-OAuth linkage.
    """

    __tablename__ = "external_identity"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider", "external_id",
            name="uq_external_identity_provider_subject",
        ),
        UniqueConstraint(
            "workspace_id", "scim_resource_id",
            name="uq_external_identity_scim_resource",
        ),
        Index("ix_external_identity_user", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[ExternalIdentityProvider] = mapped_column(
        enum_type(ExternalIdentityProvider), nullable=False
    )
    idp_entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    name_id_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    scim_resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ScimToken(WorkspaceScopedModel):
    """Per-workspace SCIM bearer credential (hash-at-rest, shown once)."""

    __tablename__ = "scim_token"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_scim_token_prefix", "token_prefix"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial, secret-safe
        return (
            f"ScimToken(id={self.id!r}, name={self.name!r}, "
            f"prefix={self.token_prefix!r}, hash=<redacted>)"
        )


class ScimGroup(WorkspaceScopedModel):
    """A SCIM-managed group; ``mapped_role`` is resolved via ``group_role_map``."""

    __tablename__ = "scim_group"
    __table_args__ = (
        UniqueConstraint("workspace_id", "scim_id", name="uq_scim_group_scim_id"),
        UniqueConstraint(
            "workspace_id", "display_name", name="uq_scim_group_display_name"
        ),
    )

    scim_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mapped_role: Mapped[UserRole | None] = mapped_column(enum_type(UserRole), nullable=True)


class ScimGroupMember(WorkspaceScopedModel):
    """Membership edge between a SCIM group and a Forge user."""

    __tablename__ = "scim_group_member"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("scim_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )


class SamlReplay(ForgeModel):
    """Postgres-fallback one-time store for SAML assertion / request ids.

    ``replay_id`` is namespaced (``assertion:<id>`` / ``authnreq:<id>``) so a
    single table backs both halves of the :class:`~forge_contracts.sso.ReplayGuard`
    protocol when Redis is unavailable. Deviation from the idealized slice doc
    (``saml_replay(assertion_id PK, …)``): the foundation mandates UUID ``id``
    PKs + timestamps on every table, so the one-time id is a UNIQUE column.
    """

    __tablename__ = "saml_replay"

    replay_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "ExternalIdentity",
    "SamlReplay",
    "ScimGroup",
    "ScimGroupMember",
    "ScimToken",
    "SsoConfiguration",
    "SsoDomain",
]
