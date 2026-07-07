"""F18 external PM-adapter models: connections, task links, webhook deliveries.

These extend the integration surface (alongside ``RepositoryConnection`` /
``MCPConnection``) with the durable state F18's bidirectional sync engine needs:

* :class:`PMConnection` — one row per (Forge project <-> external project/team).
* :class:`PMTaskLink`   — durable Forge-task <-> external-issue mapping plus the
  watermarks/hashes that drive loop suppression and conflict detection.
* :class:`PMWebhookDelivery` — inbound idempotency + audit dedup.

Secrets are **never** stored here: ``credential_ref`` / ``webhook_secret_ref``
reference the F37 vault only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import ForgeModel, WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import (
    PMAuthType,
    PMConflictPolicy,
    PMConnectionStatus,
    PMDeliveryStatus,
    PMProvider,
    PMSyncDirection,
    PMSyncState,
)


class PMConnection(WorkspaceScopedModel):
    """A connected external PM project/team synced to a Forge project."""

    __tablename__ = "pm_connection"
    __table_args__ = (
        UniqueConstraint("project_id", "provider", name="uq_pm_connection_project_provider"),
        UniqueConstraint(
            "workspace_id",
            "provider",
            "external_project_key",
            name="uq_pm_connection_workspace_provider_extkey",
        ),
        Index("ix_pm_connection_workspace_status", "workspace_id", "status"),
    )

    provider: Mapped[PMProvider] = mapped_column(enum_type(PMProvider), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    external_base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    jira_cloud_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_project_key: Mapped[str] = mapped_column(String(255), nullable=False)
    external_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_type: Mapped[PMAuthType] = mapped_column(
        enum_type(PMAuthType), default=PMAuthType.OAUTH, nullable=False
    )
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    granted_scopes: Mapped[list[str]] = mapped_column(json_type(), default=list, nullable=False)
    sync_direction: Mapped[PMSyncDirection] = mapped_column(
        enum_type(PMSyncDirection), default=PMSyncDirection.BIDIRECTIONAL, nullable=False
    )
    conflict_policy: Mapped[PMConflictPolicy] = mapped_column(
        enum_type(PMConflictPolicy), default=PMConflictPolicy.NEWEST_WINS, nullable=False
    )
    status_map: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    priority_map: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    field_map: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    webhook_secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_webhook_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outbound_cursor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outbound_cursor_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    status: Mapped[PMConnectionStatus] = mapped_column(
        enum_type(PMConnectionStatus), default=PMConnectionStatus.PENDING, nullable=False
    )
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_full_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)


class PMTaskLink(WorkspaceScopedModel):
    """Durable Forge-task <-> external-issue mapping + sync watermarks."""

    __tablename__ = "pm_task_link"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="uq_pm_task_link_conn_extid"),
        UniqueConstraint("connection_id", "forge_task_id", name="uq_pm_task_link_conn_task"),
        Index("ix_pm_task_link_workspace_state", "workspace_id", "sync_state"),
        Index("ix_pm_task_link_conn_state", "connection_id", "sync_state"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("pm_connection.id", ondelete="CASCADE"),
        nullable=False,
    )
    forge_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("task.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[PMProvider] = mapped_column(enum_type(PMProvider), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_key: Mapped[str] = mapped_column(String(255), nullable=False)
    external_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    forge_version_at_sync: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_updated_at_at_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_outbound_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_inbound_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sync_state: Mapped[PMSyncState] = mapped_column(
        enum_type(PMSyncState), default=PMSyncState.SYNCED, nullable=False
    )
    conflict_detail: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PMWebhookDelivery(ForgeModel):
    """Inbound webhook idempotency + audit dedup (not workspace-scoped; survives
    connection deletion via ``ON DELETE SET NULL``)."""

    __tablename__ = "pm_webhook_delivery"
    __table_args__ = (
        UniqueConstraint("delivery_id", name="uq_pm_webhook_delivery_delivery_id"),
        Index("ix_pm_webhook_delivery_conn_received", "connection_id", "received_at"),
    )

    provider: Mapped[PMProvider] = mapped_column(enum_type(PMProvider), nullable=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("pm_connection.id", ondelete="SET NULL"),
        nullable=True,
    )
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(default=False, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[PMDeliveryStatus] = mapped_column(
        enum_type(PMDeliveryStatus), default=PMDeliveryStatus.RECEIVED, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["PMConnection", "PMTaskLink", "PMWebhookDelivery"]
