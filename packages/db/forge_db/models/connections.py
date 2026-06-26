"""External connection models: RepositoryConnection, MCPConnection."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, enum_type, json_type
from forge_db.models.enums import (
    MCPAuthType,
    MCPIndexStrategy,
    MCPTransport,
    RepoProvider,
    SyncMode,
)


class RepositoryConnection(WorkspaceScopedModel):
    """A connected source repository (e.g. a GitHub App installation)."""

    __tablename__ = "repository_connection"

    provider: Mapped[RepoProvider] = mapped_column(
        enum_type(RepoProvider), default=RepoProvider.GITHUB, nullable=False
    )
    repo_id: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    installation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    policy_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("policy_profile.id", ondelete="SET NULL"),
        nullable=True,
    )
    config: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


class MCPConnection(WorkspaceScopedModel):
    """A registered MCP server (spec: MCP Connection Schema).

    ``allow_write`` MUST default to false; ``resource_param`` carries the RFC
    8707 ``resource`` value used to bind tokens to this server.
    """

    __tablename__ = "mcp_connection"

    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    transport: Mapped[MCPTransport] = mapped_column(
        enum_type(MCPTransport), default=MCPTransport.HTTP, nullable=False
    )
    endpoint: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    auth_type: Mapped[MCPAuthType] = mapped_column(
        enum_type(MCPAuthType), default=MCPAuthType.NONE, nullable=False
    )
    capabilities: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    sync_mode: Mapped[SyncMode] = mapped_column(
        enum_type(SyncMode), default=SyncMode.INCREMENTAL, nullable=False
    )
    index_strategy: Mapped[MCPIndexStrategy] = mapped_column(
        enum_type(MCPIndexStrategy),
        default=MCPIndexStrategy.SYNC_AND_INDEX,
        nullable=False,
    )
    freshness_sla_minutes: Mapped[int | None] = mapped_column(nullable=True)
    allow_write: Mapped[bool] = mapped_column(default=False, nullable=False)
    allowed_namespaces: Mapped[list[str]] = mapped_column(json_type(), default=list, nullable=False)
    resource_param: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
