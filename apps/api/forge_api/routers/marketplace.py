"""Marketplace router (F32) — mounted at ``/api/v1/marketplace``.

Browse/list routes require ``member`` (READ); publishing a package (``POST
/publish``, the in-app authoring flow) requires only ``WRITE`` — like the F35
benchmark submission path, any workspace member can contribute a package, not
just an admin; every *installing* mutating route (add/sync a registry,
install/update/uninstall) requires ``admin`` since it materializes a real F09/
F11 object into the workspace. All reads/writes are scoped by the authenticated
principal's ``workspace_id`` — a cross-workspace id surfaces as 404, never a
leak (AC19). The controllers are thin: they translate service errors to HTTP
status codes and DTOs, delegating all logic to
:class:`~forge_api.services.marketplace_service.MarketplaceService`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.marketplace import (
    InstallationResponse,
    InstallPlan,
    InstallRequest,
    InstallResult,
    ListingDetailResponse,
    ListingPublishRequest,
    ListingResponse,
    ListingVersionResponse,
    MarketplaceAuditResponse,
    RegistryResponse,
    RegistrySourceIn,
    RegistryUpdateIn,
)
from forge_api.services.marketplace_service import (
    HttpRegistryGateway,
    InstallationNotFoundError,
    InstallBlockedError,
    ListingNotFoundError,
    MarketplaceService,
    NameConflictError,
    PublishValidationError,
    RegistryConflictError,
    RegistryNotFoundError,
    VersionConflictError,
)
from forge_db.models.marketplace import (
    MarketplaceInstallation,
    MarketplaceListing,
    MarketplaceListingVersion,
    MarketplaceRegistry,
)
from forge_marketplace.models import ArtifactKind

router = APIRouter(
    prefix="/marketplace",
    tags=["marketplace"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


def get_marketplace_service() -> MarketplaceService:
    """Build the process-wide marketplace service (overridable in tests via DI)."""
    return MarketplaceService(
        session_factory=get_session_factory(),
        gateway=HttpRegistryGateway(),
    )


ServiceDep = Annotated[MarketplaceService, Depends(get_marketplace_service)]


# --------------------------------------------------------------------------- #
# Serializers                                                                   #
# --------------------------------------------------------------------------- #


def _registry_dto(r: MarketplaceRegistry) -> RegistryResponse:
    return RegistryResponse(
        id=r.id,
        slug=r.slug,
        name=r.name,
        type=r.type,
        url=r.url,
        ref=r.ref,
        trust_level=r.trust_level,
        enabled=r.enabled,
        has_public_key=bool(r.public_key),
        last_sync_at=r.last_sync_at,
        last_sync_status=r.last_sync_status,
        last_sync_error=r.last_sync_error,
        created_at=r.created_at,
    )


def _listing_dto(listing: MarketplaceListing, registry: MarketplaceRegistry) -> ListingResponse:
    return ListingResponse(
        id=listing.id,
        registry_id=registry.id,
        registry_slug=registry.slug,
        trust_level=registry.trust_level,
        kind=ArtifactKind(listing.kind),
        slug=listing.slug,
        name=listing.name,
        summary=listing.summary,
        tags=list(listing.tags or []),
        latest_version=listing.latest_version,
        homepage=listing.homepage,
        repository=listing.repository,
        license=listing.license,
        cached_at=listing.cached_at,
    )


def _listing_detail_dto(
    listing: MarketplaceListing,
    registry: MarketplaceRegistry,
    versions: list[MarketplaceListingVersion],
) -> ListingDetailResponse:
    base = _listing_dto(listing, registry)
    return ListingDetailResponse(
        **base.model_dump(),
        versions=[
            ListingVersionResponse(
                version=v.version,
                content_hash=v.content_hash,
                signed=bool(v.signature),
                min_forge_version=v.min_forge_version,
                published_at=v.published_at,
                yanked=v.yanked,
                yanked_reason=v.yanked_reason,
            )
            for v in versions
        ],
    )


def _installation_dto(row: MarketplaceInstallation) -> InstallationResponse:
    return InstallationResponse(
        id=row.id,
        registry_slug=row.registry_slug,
        listing_slug=row.listing_slug,
        kind=row.kind,
        installed_version=row.installed_version,
        pinned=row.pinned,
        target_kind=row.target_kind,
        target_object_id=row.target_object_id,
        content_hash=row.content_hash,
        verification_status=row.verification_status,
        status=row.status,
        available_version=row.available_version,
        yanked_reason=row.yanked_reason,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- #
# Registries                                                                    #
# --------------------------------------------------------------------------- #


@router.get("/registries", response_model=list[RegistryResponse])
def list_registries(service: ServiceDep, principal: ReaderDep) -> list[RegistryResponse]:
    rows = service.list_registries(workspace_id=principal.workspace_id)
    return [_registry_dto(r) for r in rows]


@router.post("/registries", response_model=RegistryResponse, status_code=status.HTTP_201_CREATED)
def add_registry(
    service: ServiceDep, principal: AdminDep, body: RegistrySourceIn
) -> RegistryResponse:
    try:
        registry = service.add_registry(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            name=body.name,
            type=body.type.value,
            url=body.url,
            slug=body.slug,
            ref=body.ref,
            public_key=body.public_key,
            trust_level=body.trust_level.value,
        )
    except RegistryConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _registry_dto(registry)


@router.patch("/registries/{registry_id}", response_model=RegistryResponse)
def update_registry(
    service: ServiceDep,
    principal: AdminDep,
    registry_id: uuid.UUID,
    body: RegistryUpdateIn,
) -> RegistryResponse:
    try:
        registry = service.update_registry(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            registry_id=registry_id,
            name=body.name,
            enabled=body.enabled,
            public_key=body.public_key,
            trust_level=body.trust_level.value if body.trust_level else None,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _registry_dto(registry)


@router.delete("/registries/{registry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_registry(service: ServiceDep, principal: AdminDep, registry_id: uuid.UUID) -> None:
    try:
        service.remove_registry(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            registry_id=registry_id,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/registries/{registry_id}/sync")
def sync_registry(service: ServiceDep, principal: AdminDep, registry_id: uuid.UUID) -> dict:
    try:
        report = service.sync_registry(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            registry_id=registry_id,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return report.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Catalog                                                                       #
# --------------------------------------------------------------------------- #


@router.get("/listings", response_model=list[ListingResponse])
def list_listings(
    service: ServiceDep,
    principal: ReaderDep,
    kind: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    registry_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ListingResponse]:
    rows = service.list_listings(
        workspace_id=principal.workspace_id,
        kind=kind,
        tag=tag,
        registry_id=registry_id,
        q=q,
        limit=limit,
        offset=offset,
    )
    return [_listing_dto(listing, registry) for listing, registry in rows]


@router.get("/listings/{registry_slug}/{slug}", response_model=ListingDetailResponse)
def get_listing(
    service: ServiceDep, principal: ReaderDep, registry_slug: str, slug: str
) -> ListingDetailResponse:
    try:
        listing, registry, versions = service.get_listing_detail(
            workspace_id=principal.workspace_id, registry_slug=registry_slug, slug=slug
        )
    except ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _listing_detail_dto(listing, registry, versions)


# --------------------------------------------------------------------------- #
# Publish (in-app authoring — WRITE, mirrors the offline CLI package step)      #
# --------------------------------------------------------------------------- #


@router.post("/publish", response_model=ListingDetailResponse, status_code=status.HTTP_201_CREATED)
def publish_listing(
    service: ServiceDep, principal: WriterDep, body: ListingPublishRequest
) -> ListingDetailResponse:
    try:
        listing, registry, versions = service.publish_listing(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            registry_id=body.registry_id,
            kind=body.kind,
            slug=body.slug,
            name=body.name,
            version=body.version,
            summary=body.summary,
            description=body.description,
            license=body.license,
            homepage=body.homepage,
            repository=body.repository,
            tags=body.tags,
            min_forge_version=body.min_forge_version,
            artifact=body.artifact,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PublishValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _listing_detail_dto(listing, registry, versions)


# --------------------------------------------------------------------------- #
# Preview / install                                                             #
# --------------------------------------------------------------------------- #


@router.post("/preview", response_model=InstallPlan)
def preview_install(service: ServiceDep, principal: AdminDep, body: InstallRequest) -> InstallPlan:
    try:
        return service.preview(workspace_id=principal.workspace_id, request=body)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstallBlockedError as exc:
        # A preview surfaces the block in-band; it is not an HTTP error.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc


@router.post("/install", response_model=InstallResult)
def install(service: ServiceDep, principal: AdminDep, body: InstallRequest) -> InstallResult:
    try:
        return service.install(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            actor_user_id=principal.user_id,
            request=body,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ListingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except NameConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InstallBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc


# --------------------------------------------------------------------------- #
# Installations                                                                 #
# --------------------------------------------------------------------------- #


@router.get("/installations", response_model=list[InstallationResponse])
def list_installations(service: ServiceDep, principal: ReaderDep) -> list[InstallationResponse]:
    rows = service.list_installations(workspace_id=principal.workspace_id)
    return [_installation_dto(r) for r in rows]


@router.post("/installations/{installation_id}/update", response_model=InstallResult)
def update_installation(
    service: ServiceDep,
    principal: AdminDep,
    installation_id: uuid.UUID,
    version: Annotated[str | None, Query()] = None,
) -> InstallResult:
    try:
        return service.update_installation(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            installation_id=installation_id,
            version=version,
        )
    except InstallationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (RegistryNotFoundError, ListingNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstallBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc


@router.patch("/installations/{installation_id}", response_model=InstallationResponse)
def patch_installation(
    service: ServiceDep,
    principal: AdminDep,
    installation_id: uuid.UUID,
    pinned: Annotated[bool, Query()],
) -> InstallationResponse:
    try:
        row = service.set_pin(
            workspace_id=principal.workspace_id,
            installation_id=installation_id,
            pinned=pinned,
        )
    except InstallationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _installation_dto(row)


@router.delete("/installations/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
def uninstall(service: ServiceDep, principal: AdminDep, installation_id: uuid.UUID) -> None:
    try:
        service.uninstall(
            workspace_id=principal.workspace_id,
            actor=f"user:{principal.user_id}",
            installation_id=installation_id,
        )
    except InstallationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Audit                                                                         #
# --------------------------------------------------------------------------- #


@router.get("/audit", response_model=list[MarketplaceAuditResponse])
def list_audit(
    service: ServiceDep,
    principal: AdminDep,
    limit: Annotated[int, Query(le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MarketplaceAuditResponse]:
    rows = service.list_audit(workspace_id=principal.workspace_id, limit=limit, offset=offset)
    return [
        MarketplaceAuditResponse(
            id=r.id,
            actor=r.actor,
            operation=r.operation,
            registry_slug=r.registry_slug,
            listing_slug=r.listing_slug,
            version=r.version,
            content_hash=r.content_hash,
            verification_status=r.verification_status,
            result_status=r.result_status,
            error_code=r.error_code,
            detail=r.detail if isinstance(r.detail, str) else None,
            created_at=r.created_at,
        )
        for r in rows
    ]
