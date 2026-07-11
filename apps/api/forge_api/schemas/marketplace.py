"""Request/response DTOs for the marketplace router (F32 §4).

Thin DTOs over the ``forge_marketplace`` SDK models plus DB-derived fields
(``trust_level``, ``verification_status``, ``available_version``). The install
trust-boundary DTOs (:class:`InstallRequest` / :class:`InstallPlan` /
:class:`InstallResult`) are re-exported from the SDK unchanged.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from forge_marketplace.models import (
    ArtifactKind,
    InstallPlan,
    InstallRequest,
    InstallResult,
    RegistryType,
    TrustLevel,
)

__all__ = [
    "InstallPlan",
    "InstallRequest",
    "InstallResult",
    "InstallationResponse",
    "ListingDetailResponse",
    "ListingPublishRequest",
    "ListingResponse",
    "ListingVersionResponse",
    "MarketplaceAuditResponse",
    "RegistryResponse",
    "RegistrySourceIn",
    "RegistryUpdateIn",
]


class RegistrySourceIn(BaseModel):
    """Body for ``POST /registries`` — add a registry source (admin)."""

    name: str
    type: RegistryType = RegistryType.http_index
    url: str
    slug: str | None = None
    ref: str | None = None
    public_key: str | None = None
    trust_level: TrustLevel = TrustLevel.community


class RegistryUpdateIn(BaseModel):
    """Body for ``PATCH /registries/{id}`` — enable/disable, rotate key/trust/name."""

    name: str | None = None
    enabled: bool | None = None
    public_key: str | None = None
    trust_level: TrustLevel | None = None


class RegistryResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    type: str
    url: str
    ref: str | None = None
    trust_level: str
    enabled: bool
    has_public_key: bool
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    created_at: datetime


class ListingVersionResponse(BaseModel):
    version: str
    content_hash: str
    signed: bool
    min_forge_version: str | None = None
    published_at: datetime
    yanked: bool = False
    yanked_reason: str | None = None


class ListingResponse(BaseModel):
    id: UUID
    registry_id: UUID
    registry_slug: str
    trust_level: str
    kind: ArtifactKind
    slug: str
    name: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    latest_version: str
    homepage: str | None = None
    repository: str | None = None
    license: str = "Apache-2.0"
    cached_at: datetime


class ListingDetailResponse(ListingResponse):
    versions: list[ListingVersionResponse] = Field(default_factory=list)


class ListingPublishRequest(BaseModel):
    """Body for ``POST /publish`` — the in-app equivalent of the offline

    ``forge marketplace package`` CLI authoring step: an author submits a raw
    F09 ``mcp_connector`` / F11 ``skill_profile`` artifact plus its package
    metadata directly into a registry the workspace owns. The artifact is run
    through the same authoritative installer validation the CLI applies before
    anything is persisted (schema-invalid input -> 422, never silently stored).
    """

    registry_id: UUID
    kind: ArtifactKind
    slug: str
    name: str
    version: str
    summary: str
    description: str | None = None
    license: str = "Apache-2.0"
    homepage: str | None = None
    repository: str | None = None
    tags: list[str] = Field(default_factory=list)
    min_forge_version: str | None = None
    artifact: dict


class InstallationResponse(BaseModel):
    id: UUID
    registry_slug: str
    listing_slug: str
    kind: str
    installed_version: str
    pinned: bool
    target_kind: str
    target_object_id: UUID | None = None
    content_hash: str
    verification_status: str
    status: str
    available_version: str | None = None
    yanked_reason: str | None = None
    created_at: datetime


class MarketplaceAuditResponse(BaseModel):
    id: UUID
    actor: str
    operation: str
    registry_slug: str | None = None
    listing_slug: str | None = None
    version: str | None = None
    content_hash: str | None = None
    verification_status: str | None = None
    result_status: str
    error_code: str | None = None
    detail: str | None = None
    created_at: datetime
