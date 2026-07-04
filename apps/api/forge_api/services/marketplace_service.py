"""Marketplace orchestration service (F32 §3.2).

Owns the marketplace DB records (registries / listings / versions / installations
/ domain audit) and orchestrates installs by delegating to the pure
``forge_marketplace`` SDK trust boundary (verify -> validate -> security floor)
and then materializing the local **F09 ``mcp_connection`` / F11 ``skill_profile``**
row in the *same* transaction. The service never speaks MCP, stores no secrets,
and never sets ``allow_write=true`` or triggers OAuth.

Cross-slice conformance notes (foundation vs the F32 draft):

* The install targets are the real ``mcp_connection`` / ``skill_profile`` forge_db
  rows (F09/F11 own no ``create_connection``/``create`` service in-tree; the rows
  *are* the F09/F11 objects). The MCP ``status=pending`` maps to ``is_active=false``
  (the real schema has no ``status`` column).
* Registry fetch goes through a sync :class:`RegistryGateway` seam so the router +
  service stay synchronous (matching the rest of ``apps/api``); the async
  ``forge_marketplace`` ``RegistryClient`` Protocol backs the prod HTTP gateway.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.observability.audit import AuditCategory, AuditLog
from forge_contracts.enums import MCPAuthType, MCPTransport
from forge_db.models import User
from forge_db.models.connections import MCPConnection
from forge_db.models.marketplace import (
    MarketplaceAuditLog,
    MarketplaceInstallation,
    MarketplaceListing,
    MarketplaceListingVersion,
    MarketplaceRegistry,
)
from forge_db.models.profiles import SkillProfile
from forge_marketplace.catalog import (
    find_version,
    has_newer_compatible,
    latest_compatible,
)
from forge_marketplace.installer import build_install_plan
from forge_marketplace.manifest import canonical_artifact_bytes
from forge_marketplace.models import (
    ArtifactKind,
    InstallPlan,
    InstallRequest,
    InstallResult,
    InstallStatus,
    PackageManifest,
    RegistryIndex,
    RegistryIndexEntry,
    RegistryIndexVersion,
    SyncReport,
    TrustLevel,
    VerificationStatus,
)
from forge_marketplace.registry_client import HttpIndexRegistryClient
from forge_marketplace.verifier import verify_version

DEFAULT_FORGE_VERSION = "3.0.0"


# --------------------------------------------------------------------------- #
# Service errors (mapped to HTTP status codes by the router)                    #
# --------------------------------------------------------------------------- #


class MarketplaceServiceError(Exception):
    """Base marketplace service error."""


class RegistryNotFoundError(MarketplaceServiceError):
    """No such registry in the caller's workspace (-> 404)."""


class ListingNotFoundError(MarketplaceServiceError):
    """No such listing/version (-> 404)."""


class InstallationNotFoundError(MarketplaceServiceError):
    """No such installation in the caller's workspace (-> 404)."""


class RegistryConflictError(MarketplaceServiceError):
    """Duplicate registry slug or an illegal edit to the official registry (-> 409)."""


class NameConflictError(MarketplaceServiceError):
    """Install would collide with an existing custom object (-> 409)."""


class InstallBlockedError(MarketplaceServiceError):
    """Install refused at the trust boundary (-> 422). Carries the audit code."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# Registry fetch seam (sync; backs onto the async SDK client in prod)           #
# --------------------------------------------------------------------------- #


class RegistryGateway(Protocol):
    def fetch_index(self, registry: MarketplaceRegistry) -> RegistryIndex: ...

    def fetch_manifest(
        self, registry: MarketplaceRegistry, manifest_uri: str
    ) -> tuple[PackageManifest, bytes]: ...


class HttpRegistryGateway:
    """Prod gateway: SSRF-bounded HTTP(S)/file fetch via the async SDK client."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._allowed = allowed_hosts or frozenset()
        self._timeout = timeout

    def _client(self, registry: MarketplaceRegistry) -> HttpIndexRegistryClient:
        return HttpIndexRegistryClient(
            index_url=registry.url,
            allowed_hosts=self._allowed,
            timeout=self._timeout,
        )

    def fetch_index(self, registry: MarketplaceRegistry) -> RegistryIndex:
        return asyncio.run(self._client(registry).fetch_index())

    def fetch_manifest(
        self, registry: MarketplaceRegistry, manifest_uri: str
    ) -> tuple[PackageManifest, bytes]:
        return asyncio.run(self._client(registry).fetch_manifest(manifest_uri))


def _slugify(name: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return (out or "registry")[:64]


# --------------------------------------------------------------------------- #
# Service                                                                       #
# --------------------------------------------------------------------------- #


class MarketplaceService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        gateway: RegistryGateway,
        audit: AuditLog | None = None,
        forge_version: str = DEFAULT_FORGE_VERSION,
        sync_enqueue: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        self._sf = session_factory
        self._gateway = gateway
        self._audit = audit or AuditLog()
        self._forge_version = forge_version
        self._sync_enqueue = sync_enqueue

    # ---- registries ---- #

    def list_registries(self, *, workspace_id: uuid.UUID) -> list[MarketplaceRegistry]:
        with self._sf() as s:
            rows = (
                s.execute(
                    select(MarketplaceRegistry)
                    .where(MarketplaceRegistry.workspace_id == workspace_id)
                    .order_by(MarketplaceRegistry.slug)
                )
                .scalars()
                .all()
            )
            for r in rows:
                s.expunge(r)
            return list(rows)

    def list_enabled_registries(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(workspace_id, registry_id) for every enabled registry (worker fan-out)."""
        with self._sf() as s:
            rows = s.execute(
                select(MarketplaceRegistry.workspace_id, MarketplaceRegistry.id).where(
                    MarketplaceRegistry.enabled.is_(True)
                )
            ).all()
            return [(w, i) for w, i in rows]

    def list_workspace_ids_with_installs(self) -> list[uuid.UUID]:
        """Distinct workspace ids that have marketplace installations (fan-out)."""
        with self._sf() as s:
            return list(
                s.execute(
                    select(MarketplaceInstallation.workspace_id).distinct()
                )
                .scalars()
                .all()
            )

    def sync_registry_by_id(
        self, *, registry_id: uuid.UUID, actor: str = "system"
    ) -> SyncReport:
        """System-context sync (worker): resolve the owning workspace, then sync."""
        with self._sf() as s:
            registry = s.get(MarketplaceRegistry, registry_id)
            workspace_id = registry.workspace_id if registry is not None else None
        if workspace_id is None:
            return SyncReport(registry_id=registry_id, status="error", error="registry not found")
        return self.sync_registry(
            workspace_id=workspace_id, actor=actor, registry_id=registry_id
        )

    def add_registry(
        self,
        *,
        workspace_id: uuid.UUID,
        actor: str,
        name: str,
        type: str,
        url: str,
        slug: str | None = None,
        ref: str | None = None,
        public_key: str | None = None,
        trust_level: str = TrustLevel.community.value,
    ) -> MarketplaceRegistry:
        resolved_slug = slug or _slugify(name)
        with self._sf() as s:
            existing = s.execute(
                select(MarketplaceRegistry).where(
                    MarketplaceRegistry.workspace_id == workspace_id,
                    MarketplaceRegistry.slug == resolved_slug,
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise RegistryConflictError(f"registry slug '{resolved_slug}' already exists")
            registry = MarketplaceRegistry(
                workspace_id=workspace_id,
                slug=resolved_slug,
                name=name,
                type=type,
                url=url,
                ref=ref,
                public_key=public_key,
                trust_level=trust_level,
                enabled=True,
            )
            s.add(registry)
            s.flush()
            self._write_audit(
                s,
                workspace_id=workspace_id,
                actor=actor,
                operation="registry.add",
                registry_slug=resolved_slug,
                result_status="ok",
            )
            s.commit()
            s.refresh(registry)
            s.expunge(registry)
        if self._sync_enqueue is not None:
            self._sync_enqueue(registry.id)
        return registry

    def update_registry(
        self,
        *,
        workspace_id: uuid.UUID,
        actor: str,
        registry_id: uuid.UUID,
        name: str | None = None,
        enabled: bool | None = None,
        public_key: str | None = None,
        trust_level: str | None = None,
    ) -> MarketplaceRegistry:
        with self._sf() as s:
            registry = self._require_registry(s, workspace_id, registry_id)
            if name is not None:
                registry.name = name
            if enabled is not None:
                registry.enabled = enabled
            if public_key is not None:
                registry.public_key = public_key
            if trust_level is not None:
                # The seeded official registry's trust is immutable-official.
                if registry.slug == "official" and trust_level != TrustLevel.official.value:
                    raise RegistryConflictError("cannot downgrade the official registry's trust")
                registry.trust_level = trust_level
            s.flush()
            s.commit()
            s.refresh(registry)
            s.expunge(registry)
            return registry

    def remove_registry(
        self, *, workspace_id: uuid.UUID, actor: str, registry_id: uuid.UUID
    ) -> None:
        with self._sf() as s:
            registry = self._require_registry(s, workspace_id, registry_id)
            slug = registry.slug
            s.delete(registry)  # cascades cached listings/versions
            self._write_audit(
                s,
                workspace_id=workspace_id,
                actor=actor,
                operation="registry.remove",
                registry_slug=slug,
                result_status="ok",
            )
            s.commit()

    def sync_registry(
        self, *, workspace_id: uuid.UUID, actor: str, registry_id: uuid.UUID
    ) -> SyncReport:
        """Fetch + validate the index and upsert the cached catalog (AC3)."""
        with self._sf() as s:
            registry = self._require_registry(s, workspace_id, registry_id)
            slug = registry.slug
            try:
                index = self._gateway.fetch_index(registry)
            except Exception as exc:
                registry.last_sync_at = datetime.now(UTC)
                registry.last_sync_status = "error"
                registry.last_sync_error = str(exc)[:500]
                self._write_audit(
                    s,
                    workspace_id=workspace_id,
                    actor=actor,
                    operation="registry.sync",
                    registry_slug=slug,
                    result_status="error",
                    error_code=getattr(exc, "error_code", "registry_fetch_error"),
                    detail=str(exc)[:500],
                )
                s.commit()
                return SyncReport(registry_id=registry_id, status="error", error=str(exc))

            report = self._merge_index(s, registry, index)
            registry.last_sync_at = datetime.now(UTC)
            registry.last_sync_status = "ok"
            registry.last_sync_error = None
            registry.etag = None
            self._write_audit(
                s,
                workspace_id=workspace_id,
                actor=actor,
                operation="registry.sync",
                registry_slug=slug,
                result_status="ok",
            )
            s.commit()
            return report

    def _merge_index(
        self, s: Session, registry: MarketplaceRegistry, index: RegistryIndex
    ) -> SyncReport:
        now = datetime.now(UTC)
        upserted = 0
        versions_upserted = 0
        seen_keys: set[tuple[str, str]] = set()
        for entry in index.entries:
            seen_keys.add((entry.kind.value, entry.slug))
            listing = s.execute(
                select(MarketplaceListing).where(
                    MarketplaceListing.registry_id == registry.id,
                    MarketplaceListing.kind == entry.kind.value,
                    MarketplaceListing.slug == entry.slug,
                )
            ).scalar_one_or_none()
            if listing is None:
                listing = MarketplaceListing(
                    workspace_id=registry.workspace_id,
                    registry_id=registry.id,
                    kind=entry.kind.value,
                    slug=entry.slug,
                    name=entry.name,
                    summary=entry.summary,
                    tags=list(entry.tags),
                    latest_version=entry.latest_version,
                    homepage=entry.homepage,
                    repository=entry.repository,
                    license=entry.license,
                    cached_at=now,
                )
                s.add(listing)
                s.flush()
            else:
                listing.name = entry.name
                listing.summary = entry.summary
                listing.tags = list(entry.tags)
                listing.latest_version = entry.latest_version
                listing.homepage = entry.homepage
                listing.repository = entry.repository
                listing.license = entry.license
                listing.cached_at = now
            upserted += 1
            versions_upserted += self._merge_versions(s, listing, entry)

        # Prune listings removed upstream.
        pruned = 0
        existing = (
            s.execute(
                select(MarketplaceListing).where(
                    MarketplaceListing.registry_id == registry.id
                )
            )
            .scalars()
            .all()
        )
        for listing in existing:
            if (listing.kind, listing.slug) not in seen_keys:
                s.delete(listing)
                pruned += 1
        s.flush()
        return SyncReport(
            registry_id=registry.id,
            listings_upserted=upserted,
            versions_upserted=versions_upserted,
            listings_pruned=pruned,
            status="ok",
        )

    def _merge_versions(
        self, s: Session, listing: MarketplaceListing, entry: RegistryIndexEntry
    ) -> int:
        count = 0
        seen: set[str] = set()
        for v in entry.versions:
            seen.add(v.version)
            row = s.execute(
                select(MarketplaceListingVersion).where(
                    MarketplaceListingVersion.listing_id == listing.id,
                    MarketplaceListingVersion.version == v.version,
                )
            ).scalar_one_or_none()
            if row is None:
                s.add(
                    MarketplaceListingVersion(
                        listing_id=listing.id,
                        version=v.version,
                        content_hash=v.content_hash,
                        manifest_hash=v.manifest_hash,
                        signature=v.signature,
                        manifest_uri=v.manifest_uri,
                        min_forge_version=v.min_forge_version,
                        published_at=v.published_at,
                        yanked=v.yanked,
                        yanked_reason=v.yanked_reason,
                    )
                )
            else:
                row.content_hash = v.content_hash
                row.manifest_hash = v.manifest_hash
                row.signature = v.signature
                row.manifest_uri = v.manifest_uri
                row.min_forge_version = v.min_forge_version
                row.published_at = v.published_at
                row.yanked = v.yanked
                row.yanked_reason = v.yanked_reason
            count += 1
        # Prune versions removed upstream.
        for row in (
            s.execute(
                select(MarketplaceListingVersion).where(
                    MarketplaceListingVersion.listing_id == listing.id
                )
            )
            .scalars()
            .all()
        ):
            if row.version not in seen:
                s.delete(row)
        s.flush()
        return count

    # ---- catalog browse ---- #

    def list_listings(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str | None = None,
        tag: str | None = None,
        registry_id: uuid.UUID | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[tuple[MarketplaceListing, MarketplaceRegistry]]:
        with self._sf() as s:
            stmt = (
                select(MarketplaceListing, MarketplaceRegistry)
                .join(
                    MarketplaceRegistry,
                    MarketplaceListing.registry_id == MarketplaceRegistry.id,
                )
                .where(MarketplaceListing.workspace_id == workspace_id)
            )
            if kind:
                stmt = stmt.where(MarketplaceListing.kind == kind)
            if registry_id:
                stmt = stmt.where(MarketplaceListing.registry_id == registry_id)
            if q:
                like = f"%{q.lower()}%"
                stmt = stmt.where(
                    func.lower(MarketplaceListing.name).like(like)
                    | func.lower(MarketplaceListing.summary).like(like)
                )
            stmt = stmt.order_by(MarketplaceListing.name).limit(limit).offset(offset)
            rows = s.execute(stmt).all()
            out: list[tuple[MarketplaceListing, MarketplaceRegistry]] = []
            for listing, registry in rows:
                if tag and tag not in (listing.tags or []):
                    continue
                s.expunge(listing)
                s.expunge(registry)
                out.append((listing, registry))
            return out

    def get_listing_detail(
        self, *, workspace_id: uuid.UUID, registry_slug: str, slug: str
    ) -> tuple[MarketplaceListing, MarketplaceRegistry, list[MarketplaceListingVersion]]:
        with self._sf() as s:
            row = s.execute(
                select(MarketplaceListing, MarketplaceRegistry)
                .join(
                    MarketplaceRegistry,
                    MarketplaceListing.registry_id == MarketplaceRegistry.id,
                )
                .where(
                    MarketplaceListing.workspace_id == workspace_id,
                    MarketplaceRegistry.slug == registry_slug,
                    MarketplaceListing.slug == slug,
                )
            ).first()
            if row is None:
                raise ListingNotFoundError(f"listing '{registry_slug}/{slug}' not found")
            listing, registry = row
            versions = (
                s.execute(
                    select(MarketplaceListingVersion)
                    .where(MarketplaceListingVersion.listing_id == listing.id)
                    .order_by(MarketplaceListingVersion.published_at.desc())
                )
                .scalars()
                .all()
            )
            for v in versions:
                s.expunge(v)
            s.expunge(listing)
            s.expunge(registry)
            return listing, registry, list(versions)

    # ---- preview / install ---- #

    def preview(self, *, workspace_id: uuid.UUID, request: InstallRequest) -> InstallPlan:
        """Side-effect-free fetch + verify + validate (AC11)."""
        with self._sf() as s:
            registry = self._require_registry(s, workspace_id, request.registry_id)
            _, plan = self._fetch_and_plan(registry, request)
            return plan

    def install(
        self, *, workspace_id: uuid.UUID, actor: str, actor_user_id: uuid.UUID | None,
        request: InstallRequest,
    ) -> InstallResult:
        with self._sf() as s:
            registry = self._require_registry(s, workspace_id, request.registry_id)
            manifest, plan = self._fetch_and_plan(registry, request)
            self._enforce_install_policy(
                s, workspace_id, actor, registry, plan, request
            )
            result = self._apply(
                s,
                workspace_id=workspace_id,
                actor=actor,
                actor_user_id=actor_user_id,
                registry=registry,
                manifest=manifest,
                plan=plan,
                request=request,
            )
            s.commit()
            return result

    def _fetch_and_plan(
        self, registry: MarketplaceRegistry, request: InstallRequest
    ) -> tuple[PackageManifest, InstallPlan]:
        index = self._gateway.fetch_index(registry)
        entry = next(
            (
                e
                for e in index.entries
                if e.kind == request.kind and e.slug == request.slug
            ),
            None,
        )
        if entry is None:
            raise ListingNotFoundError(
                f"package '{request.kind.value}/{request.slug}' not found in registry"
            )
        version = self._resolve_version(entry, request.version)
        manifest, _raw = self._gateway.fetch_manifest(registry, version.manifest_uri)
        verification = verify_version(
            artifact_bytes=canonical_artifact_bytes(manifest.artifact),
            version=version,
            registry_public_key=registry.public_key,
        )
        plan = build_install_plan(
            manifest=manifest,
            version=version.version,
            verification=verification,
            forge_version=self._forge_version,
            registry_id=registry.id,
            override_name=request.override_name,
        )
        if version.yanked:
            plan.warnings.append(
                f"Yanked: {version.yanked_reason or 'this version was withdrawn'}"
            )
        return manifest, plan

    def _resolve_version(
        self, entry: RegistryIndexEntry, version: str | None
    ) -> RegistryIndexVersion:
        if version is None:
            resolved = latest_compatible(entry.versions, forge_version=self._forge_version)
            if resolved is None:
                raise InstallBlockedError(
                    "no compatible non-yanked version available",
                    error_code="forge_version_incompatible",
                )
            return resolved
        found = find_version(entry.versions, version)
        if found is None:
            raise ListingNotFoundError(f"version '{version}' not found")
        return found

    def _enforce_install_policy(
        self,
        s: Session,
        workspace_id: uuid.UUID,
        actor: str,
        registry: MarketplaceRegistry,
        plan: InstallPlan,
        request: InstallRequest,
    ) -> None:
        if plan.blocked:
            code = self._block_code(plan)
            self._audit_denied(s, workspace_id, actor, registry, plan, code)
            s.commit()
            raise InstallBlockedError(plan.block_reason or "install blocked", error_code=code)

        strict = registry.trust_level in (TrustLevel.official.value, TrustLevel.trusted.value)
        if strict and plan.verification.status is not VerificationStatus.verified:
            self._audit_denied(s, workspace_id, actor, registry, plan, "signature_required")
            s.commit()
            raise InstallBlockedError(
                f"{registry.trust_level} registries require a verified signature",
                error_code="signature_required",
            )
        if plan.verification.needs_acknowledgement and not request.acknowledge_unverified:
            self._audit_denied(s, workspace_id, actor, registry, plan, "signature_required")
            s.commit()
            raise InstallBlockedError(
                "package is unverified — acknowledge_unverified is required",
                error_code="signature_required",
            )

    @staticmethod
    def _block_code(plan: InstallPlan) -> str:
        status = plan.verification.status
        if status is VerificationStatus.hash_mismatch:
            return "hash_mismatch"
        if status is VerificationStatus.signature_invalid:
            return "signature_invalid"
        reason = (plan.block_reason or "").lower()
        if "requires forge" in reason:
            return "forge_version_incompatible"
        return "schema_invalid"

    def _apply(
        self,
        s: Session,
        *,
        workspace_id: uuid.UUID,
        actor: str,
        actor_user_id: uuid.UUID | None,
        registry: MarketplaceRegistry,
        manifest: PackageManifest,
        plan: InstallPlan,
        request: InstallRequest,
    ) -> InstallResult:
        if manifest.kind is ArtifactKind.skill_profile:
            target_kind, target_id = self._apply_skill(
                s, workspace_id, plan, request, existing=None
            )
        else:
            target_kind, target_id = self._apply_mcp(s, workspace_id, manifest, plan)

        installation = self._upsert_installation(
            s,
            workspace_id=workspace_id,
            registry=registry,
            manifest=manifest,
            plan=plan,
            target_kind=target_kind,
            target_id=target_id,
            actor_user_id=actor_user_id,
        )
        self._write_audit(
            s,
            workspace_id=workspace_id,
            actor=actor,
            operation="install",
            registry_slug=registry.slug,
            listing_slug=manifest.slug,
            version=plan.version,
            content_hash=manifest.content_hash,
            verification_status=plan.verification.status.value,
            result_status="ok",
        )
        return InstallResult(
            installation_id=installation.id,
            target_kind=target_kind,
            target_object_id=target_id,
            status=InstallStatus(installation.status),
            version=plan.version,
            verification=plan.verification,
            warnings=plan.warnings,
        )

    def _apply_skill(
        self,
        s: Session,
        workspace_id: uuid.UUID,
        plan: InstallPlan,
        request: InstallRequest,
        *,
        existing: SkillProfile | None,
    ) -> tuple[str, uuid.UUID]:
        cfg = dict(plan.resolved_config)
        name = request.override_name or cfg.get("name")
        if not name:
            raise InstallBlockedError("skill profile has no name", error_code="schema_invalid")
        cfg["name"] = name
        if existing is None:
            collision = s.execute(
                select(SkillProfile).where(
                    SkillProfile.workspace_id == workspace_id, SkillProfile.name == name
                )
            ).scalar_one_or_none()
            if collision is not None:
                raise NameConflictError(
                    f"a custom skill profile named '{name}' already exists; "
                    "supply override_name or update the existing installation"
                )
            profile = SkillProfile(
                workspace_id=workspace_id,
                name=name,
                description=cfg.get("description"),
                behavior=cfg,
            )
            s.add(profile)
            s.flush()
            return "skill_profile", profile.id
        existing.description = cfg.get("description")
        existing.behavior = cfg
        s.flush()
        return "skill_profile", existing.id

    def _apply_mcp(
        self,
        s: Session,
        workspace_id: uuid.UUID,
        manifest: PackageManifest,
        plan: InstallPlan,
        *,
        existing: MCPConnection | None = None,
    ) -> tuple[str, uuid.UUID]:
        cfg = dict(plan.resolved_config)
        transport = MCPTransport(cfg.get("transport", "http"))
        namespaces = list(cfg.get("allowed_namespaces") or [])
        if existing is None:
            conn = MCPConnection(
                workspace_id=workspace_id,
                slug=manifest.slug,
                name=cfg.get("name") or manifest.name,
                transport=transport,
                endpoint=cfg.get("endpoint"),
                auth_type=MCPAuthType.NONE,
                allow_write=False,  # security floor — never writable at install
                allowed_namespaces=namespaces,
                is_active=False,  # "pending": never auto-connected / OAuth'd
            )
            s.add(conn)
            s.flush()
            return "mcp_connection", conn.id
        # Update: scope/endpoint may change -> reset to pending (not-connected).
        existing.name = cfg.get("name") or existing.name
        existing.endpoint = cfg.get("endpoint")
        existing.transport = transport
        existing.allowed_namespaces = namespaces
        existing.allow_write = False
        existing.is_active = False
        s.flush()
        return "mcp_connection", existing.id

    def _upsert_installation(
        self,
        s: Session,
        *,
        workspace_id: uuid.UUID,
        registry: MarketplaceRegistry,
        manifest: PackageManifest,
        plan: InstallPlan,
        target_kind: str,
        target_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
    ) -> MarketplaceInstallation:
        row = s.execute(
            select(MarketplaceInstallation).where(
                MarketplaceInstallation.workspace_id == workspace_id,
                MarketplaceInstallation.registry_slug == registry.slug,
                MarketplaceInstallation.listing_slug == manifest.slug,
            )
        ).scalar_one_or_none()
        status = InstallStatus.installed.value
        if row is None:
            row = MarketplaceInstallation(
                workspace_id=workspace_id,
                registry_slug=registry.slug,
                listing_slug=manifest.slug,
                kind=manifest.kind.value,
                installed_version=plan.version,
                target_kind=target_kind,
                target_object_id=target_id,
                content_hash=manifest.content_hash,
                verification_status=plan.verification.status.value,
                status=status,
                available_version=None,
                yanked_reason=None,
                installed_by=actor_user_id,
            )
            s.add(row)
        else:
            row.installed_version = plan.version
            row.kind = manifest.kind.value
            row.target_kind = target_kind
            row.target_object_id = target_id
            row.content_hash = manifest.content_hash
            row.verification_status = plan.verification.status.value
            row.status = status
            row.available_version = None
            row.yanked_reason = None
        s.flush()
        return row

    # ---- installations ---- #

    def list_installations(
        self, *, workspace_id: uuid.UUID
    ) -> list[MarketplaceInstallation]:
        with self._sf() as s:
            rows = (
                s.execute(
                    select(MarketplaceInstallation)
                    .where(MarketplaceInstallation.workspace_id == workspace_id)
                    .order_by(MarketplaceInstallation.listing_slug)
                )
                .scalars()
                .all()
            )
            for r in rows:
                s.expunge(r)
            return list(rows)

    def set_pin(
        self, *, workspace_id: uuid.UUID, installation_id: uuid.UUID, pinned: bool
    ) -> MarketplaceInstallation:
        with self._sf() as s:
            row = self._require_installation(s, workspace_id, installation_id)
            row.pinned = pinned
            if pinned and row.status == InstallStatus.update_available.value:
                row.status = InstallStatus.installed.value
                row.available_version = None
            s.flush()
            s.commit()
            s.refresh(row)
            s.expunge(row)
            return row

    def update_installation(
        self, *, workspace_id: uuid.UUID, actor: str, installation_id: uuid.UUID,
        version: str | None = None,
    ) -> InstallResult:
        with self._sf() as s:
            row = self._require_installation(s, workspace_id, installation_id)
            registry = s.execute(
                select(MarketplaceRegistry).where(
                    MarketplaceRegistry.workspace_id == workspace_id,
                    MarketplaceRegistry.slug == row.registry_slug,
                )
            ).scalar_one_or_none()
            if registry is None:
                raise RegistryNotFoundError("source registry no longer exists")
            target_version = version or row.available_version
            request = InstallRequest(
                registry_id=registry.id,
                kind=ArtifactKind(row.kind),
                slug=row.listing_slug,
                version=target_version,
                acknowledge_unverified=True,  # update path re-verifies + re-validates
            )
            manifest, plan = self._fetch_and_plan(registry, request)
            self._enforce_install_policy(s, workspace_id, actor, registry, plan, request)

            if manifest.kind is ArtifactKind.skill_profile:
                existing = (
                    s.get(SkillProfile, row.target_object_id)
                    if row.target_object_id
                    else None
                )
                target_kind, target_id = self._apply_skill(
                    s, workspace_id, plan, request, existing=existing
                )
            else:
                existing_conn = (
                    s.get(MCPConnection, row.target_object_id)
                    if row.target_object_id
                    else None
                )
                target_kind, target_id = self._apply_mcp(
                    s, workspace_id, manifest, plan, existing=existing_conn
                )

            row.installed_version = plan.version
            row.content_hash = manifest.content_hash
            row.verification_status = plan.verification.status.value
            row.status = InstallStatus.installed.value
            row.available_version = None
            row.yanked_reason = None
            row.target_kind = target_kind
            row.target_object_id = target_id
            self._write_audit(
                s,
                workspace_id=workspace_id,
                actor=actor,
                operation="update",
                registry_slug=registry.slug,
                listing_slug=row.listing_slug,
                version=plan.version,
                content_hash=manifest.content_hash,
                verification_status=plan.verification.status.value,
                result_status="ok",
            )
            s.commit()
            return InstallResult(
                installation_id=row.id,
                target_kind=target_kind,
                target_object_id=target_id,
                status=InstallStatus.installed,
                version=plan.version,
                verification=plan.verification,
                warnings=plan.warnings,
            )

    def uninstall(
        self, *, workspace_id: uuid.UUID, actor: str, installation_id: uuid.UUID
    ) -> None:
        with self._sf() as s:
            row = self._require_installation(s, workspace_id, installation_id)
            if row.target_object_id is not None:
                if row.target_kind == "skill_profile":
                    obj = s.get(SkillProfile, row.target_object_id)
                else:
                    obj = s.get(MCPConnection, row.target_object_id)
                if obj is not None:
                    s.delete(obj)
            row.status = InstallStatus.uninstalled.value
            row.target_object_id = None
            row.available_version = None
            self._write_audit(
                s,
                workspace_id=workspace_id,
                actor=actor,
                operation="uninstall",
                registry_slug=row.registry_slug,
                listing_slug=row.listing_slug,
                version=row.installed_version,
                result_status="ok",
            )
            s.commit()

    def refresh_update_flags(self, *, workspace_id: uuid.UUID) -> int:
        """Flag installed items with a newer compatible version / yank (AC13/15)."""
        flagged = 0
        with self._sf() as s:
            installs = (
                s.execute(
                    select(MarketplaceInstallation).where(
                        MarketplaceInstallation.workspace_id == workspace_id,
                        MarketplaceInstallation.status.in_(
                            [
                                InstallStatus.installed.value,
                                InstallStatus.update_available.value,
                            ]
                        ),
                    )
                )
                .scalars()
                .all()
            )
            for inst in installs:
                versions = self._cached_versions(s, workspace_id, inst)
                installed = find_version(versions, inst.installed_version)
                if installed is not None and installed.yanked:
                    inst.yanked_reason = installed.yanked_reason or "withdrawn"
                if inst.pinned:
                    continue
                newer = has_newer_compatible(
                    installed_version=inst.installed_version,
                    versions=versions,
                    forge_version=self._forge_version,
                )
                if newer is not None:
                    inst.status = InstallStatus.update_available.value
                    inst.available_version = newer.version
                    flagged += 1
            s.commit()
        return flagged

    def _cached_versions(
        self, s: Session, workspace_id: uuid.UUID, inst: MarketplaceInstallation
    ) -> list[RegistryIndexVersion]:
        rows = (
            s.execute(
                select(MarketplaceListingVersion)
                .join(
                    MarketplaceListing,
                    MarketplaceListingVersion.listing_id == MarketplaceListing.id,
                )
                .join(
                    MarketplaceRegistry,
                    MarketplaceListing.registry_id == MarketplaceRegistry.id,
                )
                .where(
                    MarketplaceListing.workspace_id == workspace_id,
                    MarketplaceRegistry.slug == inst.registry_slug,
                    MarketplaceListing.slug == inst.listing_slug,
                )
            )
            .scalars()
            .all()
        )
        return [
            RegistryIndexVersion(
                version=r.version,
                content_hash=r.content_hash,
                manifest_hash=r.manifest_hash,
                signature=r.signature,
                manifest_uri=r.manifest_uri,
                min_forge_version=r.min_forge_version,
                published_at=r.published_at,
                yanked=r.yanked,
                yanked_reason=r.yanked_reason,
            )
            for r in rows
        ]

    # ---- audit ---- #

    def list_audit(
        self, *, workspace_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> list[MarketplaceAuditLog]:
        with self._sf() as s:
            rows = (
                s.execute(
                    select(MarketplaceAuditLog)
                    .where(MarketplaceAuditLog.workspace_id == workspace_id)
                    .order_by(MarketplaceAuditLog.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
            for r in rows:
                s.expunge(r)
            return list(rows)

    # ---- helpers ---- #

    def _require_registry(
        self, s: Session, workspace_id: uuid.UUID, registry_id: uuid.UUID
    ) -> MarketplaceRegistry:
        registry = s.execute(
            select(MarketplaceRegistry).where(
                MarketplaceRegistry.workspace_id == workspace_id,
                MarketplaceRegistry.id == registry_id,
            )
        ).scalar_one_or_none()
        if registry is None:
            raise RegistryNotFoundError("registry not found")
        return registry

    def _require_installation(
        self, s: Session, workspace_id: uuid.UUID, installation_id: uuid.UUID
    ) -> MarketplaceInstallation:
        row = s.execute(
            select(MarketplaceInstallation).where(
                MarketplaceInstallation.workspace_id == workspace_id,
                MarketplaceInstallation.id == installation_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise InstallationNotFoundError("installation not found")
        return row

    def _audit_denied(
        self,
        s: Session,
        workspace_id: uuid.UUID,
        actor: str,
        registry: MarketplaceRegistry,
        plan: InstallPlan,
        error_code: str,
    ) -> None:
        self._write_audit(
            s,
            workspace_id=workspace_id,
            actor=actor,
            operation="install",
            registry_slug=registry.slug,
            listing_slug=plan.slug,
            version=plan.version,
            verification_status=plan.verification.status.value,
            result_status="denied",
            error_code=error_code,
            detail=plan.block_reason,
        )

    def _write_audit(
        self,
        s: Session,
        *,
        workspace_id: uuid.UUID,
        actor: str,
        operation: str,
        result_status: str,
        registry_slug: str | None = None,
        listing_slug: str | None = None,
        version: str | None = None,
        content_hash: str | None = None,
        verification_status: str | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        s.add(
            MarketplaceAuditLog(
                workspace_id=workspace_id,
                actor=actor,
                operation=operation,
                registry_slug=registry_slug,
                listing_slug=listing_slug,
                version=version,
                content_hash=content_hash,
                verification_status=verification_status,
                result_status=result_status,
                error_code=error_code,
                detail=detail,
            )
        )
        s.flush()
        # Also emit a compact event through the canonical AuditSink (F39).
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action=f"marketplace.{operation}",
            actor=actor,
            workspace_id=workspace_id,
            status=result_status,
            detail=detail,
            metadata={
                "registry": registry_slug,
                "listing": listing_slug,
                "version": version,
                "error_code": error_code,
            },
        )


def seed_official_registry(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    url: str,
    public_key: str | None,
) -> MarketplaceRegistry | None:
    """Seed (idempotently) the read-only ``official`` registry for a workspace (AC2)."""
    existing = session.execute(
        select(MarketplaceRegistry).where(
            MarketplaceRegistry.workspace_id == workspace_id,
            MarketplaceRegistry.slug == "official",
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    registry = MarketplaceRegistry(
        workspace_id=workspace_id,
        slug="official",
        name="Forge Official Marketplace",
        type="http_index",
        url=url,
        public_key=public_key,
        trust_level=TrustLevel.official.value,
        enabled=True,
    )
    session.add(registry)
    session.flush()
    return registry


def backfill_official_registries(
    session: Session, *, url: str, public_key: str | None
) -> int:
    """Seed the official registry for every existing workspace (startup hook)."""
    count = 0
    workspace_ids = session.execute(select(User.workspace_id).distinct()).scalars().all()
    for ws_id in {*workspace_ids}:
        if seed_official_registry(session, workspace_id=ws_id, url=url, public_key=public_key):
            count += 1
    session.commit()
    return count


__all__ = [
    "HttpRegistryGateway",
    "InstallBlockedError",
    "InstallationNotFoundError",
    "ListingNotFoundError",
    "MarketplaceService",
    "MarketplaceServiceError",
    "NameConflictError",
    "RegistryConflictError",
    "RegistryGateway",
    "RegistryNotFoundError",
    "backfill_official_registries",
    "seed_official_registry",
]
