"""Integration-marketplace models (F32).

Five workspace-scoped tables forming the catalog + provenance + audit trail for
installing community MCP connectors & skill profiles:

* :class:`MarketplaceRegistry` — a trusted registry source (official + community).
* :class:`MarketplaceListing` — a cached catalog entry (one per package/registry).
* :class:`MarketplaceListingVersion` — per-version metadata cached from the index.
* :class:`MarketplaceInstallation` — what is installed + immutable provenance.
* :class:`MarketplaceAuditLog` — append-only per-domain audit (F39 immutability).

Foundation conformance (deviations from the F32 draft, which used plural
table names / a bespoke ``apps/api/alembic`` tree): tables are **singular** to
match every existing forge_db table (``mcp_connection``, ``skill_profile``,
``audit_log`` …), enum-like columns are stored as ``String`` values (no coupling
of forge_db to the ``forge_marketplace`` enums), and the migration lives in the
canonical ``packages/db/migrations`` chain. The MCP-connector install "status"
maps onto the real ``mcp_connection`` schema, which has no ``status`` column, via
``is_active=false`` (pending / not-connected).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from forge_db.base import (
    ForgeModel,
    WorkspaceScopedModel,
    attach_immutability_trigger,
    json_type,
)


class MarketplaceRegistry(WorkspaceScopedModel):
    """A trusted marketplace registry source (a signed ``index.json`` at a URL)."""

    __tablename__ = "marketplace_registry"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_marketplace_registry_slug"),
        CheckConstraint("type IN ('git','http_index')", name="registry_type"),
        CheckConstraint(
            "trust_level IN ('official','trusted','community','unverified')",
            name="registry_trust_level",
        ),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    trust_level: Mapped[str] = mapped_column(String(16), default="community", nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketplaceListing(WorkspaceScopedModel):
    """A cached catalog entry (one row per package per registry)."""

    __tablename__ = "marketplace_listing"
    __table_args__ = (
        UniqueConstraint(
            "registry_id", "kind", "slug", name="uq_marketplace_listing_registry_kind_slug"
        ),
        Index("ix_marketplace_listing_workspace_kind", "workspace_id", "kind"),
    )

    registry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("marketplace_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(json_type(), default=list, nullable=False)
    latest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    homepage: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    repository: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    license: Mapped[str] = mapped_column(String(64), default="Apache-2.0", nullable=False)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketplaceListingVersion(ForgeModel):
    """Per-version metadata cached from a registry index."""

    __tablename__ = "marketplace_listing_version"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "version", name="uq_marketplace_listing_version_listing_version"
        ),
        Index("ix_marketplace_listing_version_published", "listing_id", "published_at"),
    )

    listing_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("marketplace_listing.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    min_forge_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    yanked: Mapped[bool] = mapped_column(default=False, nullable=False)
    yanked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketplaceInstallation(WorkspaceScopedModel):
    """What is installed into a workspace + immutable provenance snapshot."""

    __tablename__ = "marketplace_installation"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "registry_slug",
            "listing_slug",
            name="uq_marketplace_installation_pkg",
        ),
        Index("ix_marketplace_installation_workspace_status", "workspace_id", "status"),
    )

    registry_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    listing_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    installed_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pinned: Mapped[bool] = mapped_column(default=False, nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    available_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    yanked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    installed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )


class MarketplaceAuditLog(WorkspaceScopedModel):
    """Immutable, append-only per-domain marketplace audit record (F39 pattern)."""

    __tablename__ = "marketplace_audit_log"
    __table_args__ = (
        Index(
            "ix_marketplace_audit_log_ws_op_created",
            "workspace_id",
            "operation",
            "created_at",
        ),
    )

    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    registry_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listing_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    verification_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any] | str | None] = mapped_column(Text, nullable=True)


# Append-only hardening on Postgres (no-op on SQLite unit tests) — same reusable
# F39 helper the F09/F29/F31 domain audit tables opt into.
attach_immutability_trigger(MarketplaceAuditLog.__table__)


def _pg_ddl(table: str, name: str, create_sql: str) -> None:
    """Register a Postgres-only index (GIN/full-text) via create_all + Alembic.

    Applied on the table's ``after_create`` and guarded to the ``postgresql``
    dialect, so it lands on real Postgres (both ``create_all`` and the migration)
    and is a clean no-op on the SQLite unit-test dialect.
    """
    create = DDL(create_sql)
    drop = DDL(f"DROP INDEX IF EXISTS {name};")
    tbl = {
        "marketplace_listing": MarketplaceListing,
    }[table].__table__
    event.listen(tbl, "after_create", create.execute_if(dialect="postgresql"))
    event.listen(tbl, "before_drop", drop.execute_if(dialect="postgresql"))


# GIN index on tags + a full-text index over (name || ' ' || summary) so catalog
# search is index-backed on Postgres (F32 §3.1). Portable ILIKE fallback covers
# the SQLite unit path.
_pg_ddl(
    "marketplace_listing",
    "ix_marketplace_listing_tags_gin",
    "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_tags_gin "
    "ON marketplace_listing USING gin (tags jsonb_path_ops);",
)
_pg_ddl(
    "marketplace_listing",
    "ix_marketplace_listing_fts",
    "CREATE INDEX IF NOT EXISTS ix_marketplace_listing_fts "
    "ON marketplace_listing USING gin "
    "(to_tsvector('english', name || ' ' || summary));",
)


__all__ = [
    "MarketplaceAuditLog",
    "MarketplaceInstallation",
    "MarketplaceListing",
    "MarketplaceListingVersion",
    "MarketplaceRegistry",
]
