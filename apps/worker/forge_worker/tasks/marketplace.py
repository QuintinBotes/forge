"""Marketplace catalog-sync + update-flag worker tasks (F32 §3.3), queue ``marketplace``.

Thin Celery seams over the :class:`~forge_api.services.marketplace_service.MarketplaceService`
so the heavy logic (fetch/validate/upsert/prune, semver update flags) is unit-tested
once in the service. Catalog sync fetches only the index (SSRF-bounded, size-capped) —
the full artifact is verified lazily at ``/preview`` + ``/install`` — so one failing
registry never fails another (each ``sync_registry`` is isolated).

No LangGraph / agent involvement — the marketplace is admin tooling.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_session_factory
from forge_api.services.marketplace_service import (
    HttpRegistryGateway,
    MarketplaceService,
    RegistryGateway,
    backfill_official_registries,
)
from forge_worker.celery_app import celery_app


def _allowed_hosts() -> frozenset[str]:
    raw = os.environ.get("MARKETPLACE_ALLOWED_REGISTRY_HOSTS", "")
    return frozenset(h.strip() for h in raw.split(",") if h.strip())


def build_service(
    session_factory: sessionmaker[Session] | None = None,
    gateway: RegistryGateway | None = None,
) -> MarketplaceService:
    """Construct the marketplace service for the worker (overridable in tests)."""
    return MarketplaceService(
        session_factory=session_factory or get_session_factory(),
        gateway=gateway or HttpRegistryGateway(allowed_hosts=_allowed_hosts()),
    )


def sync_registry_core(service: MarketplaceService, registry_id: uuid.UUID) -> dict:
    """Deterministic core (no Celery): sync one registry. Returns the report dict."""
    return service.sync_registry_by_id(registry_id=registry_id).model_dump(mode="json")


def sync_all_core(service: MarketplaceService) -> int:
    """Fan out ``sync_registry`` to every enabled registry. Returns the count synced."""
    pairs = service.list_enabled_registries()
    for _workspace_id, registry_id in pairs:
        service.sync_registry_by_id(registry_id=registry_id)
    return len(pairs)


def refresh_update_flags_core(service: MarketplaceService) -> int:
    """Recompute update/yank flags for every workspace with installs."""
    flagged = 0
    for workspace_id in service.list_workspace_ids_with_installs():
        flagged += service.refresh_update_flags(workspace_id=workspace_id)
    return flagged


@celery_app.task(name="marketplace.sync_registry", queue="marketplace")
def sync_registry(registry_id: str) -> dict:
    return sync_registry_core(build_service(), uuid.UUID(registry_id))


@celery_app.task(name="marketplace.sync_all_registries", queue="marketplace")
def sync_all_registries() -> int:
    return sync_all_core(build_service())


@celery_app.task(name="marketplace.refresh_update_flags", queue="marketplace")
def refresh_update_flags() -> int:
    return refresh_update_flags_core(build_service())


@celery_app.task(name="marketplace.backfill_official_registries", queue="marketplace")
def backfill_official() -> int:
    """Seed the per-workspace official registry for existing workspaces (AC2)."""
    url = os.environ.get("MARKETPLACE_OFFICIAL_REGISTRY_URL")
    if not url:
        return 0
    pubkey = os.environ.get("MARKETPLACE_OFFICIAL_REGISTRY_PUBKEY") or None
    with get_session_factory()() as session:
        return backfill_official_registries(session, url=url, public_key=pubkey)


__all__ = [
    "build_service",
    "refresh_update_flags",
    "refresh_update_flags_core",
    "sync_all_core",
    "sync_all_registries",
    "sync_registry",
    "sync_registry_core",
]
