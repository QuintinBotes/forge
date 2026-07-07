"""Catalog sync, browse, update, uninstall, and audit tests (AC3/13/15/16/18)."""

from __future__ import annotations

import uuid

from conftest import (
    WS_ID,
    FakeGateway,
    Keypair,
    make_mcp_version,
    make_skill_version,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from forge_api.services.marketplace_service import MarketplaceService
from forge_db.models.connections import MCPConnection
from forge_db.models.marketplace import (
    MarketplaceInstallation,
    MarketplaceListing,
    MarketplaceListingVersion,
)
from forge_db.models.profiles import SkillProfile


def test_sync_upserts_and_prunes(
    service: MarketplaceService, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    m1, v1 = make_skill_version(keypair, slug="alpha", name="alpha")
    m2, v2 = make_mcp_version(keypair, slug="beta", name="beta")
    gateway.add(m1, v1)
    gateway.add(m2, v2)
    report = service.sync_registry(workspace_id=WS_ID, actor="user:x", registry_id=registry.id)
    assert report.status == "ok"
    assert report.listings_upserted == 2

    with session_factory() as s:
        listings = s.execute(select(MarketplaceListing)).scalars().all()
        assert {left.slug for left in listings} == {"alpha", "beta"}
        assert s.execute(select(MarketplaceListingVersion)).scalars().all()

    # Drop 'beta' upstream and resync -> pruned.
    gateway._packages.pop((m2.kind, m2.slug))
    gateway._manifests.pop(v2.manifest_uri)
    report2 = service.sync_registry(workspace_id=WS_ID, actor="user:x", registry_id=registry.id)
    assert report2.listings_pruned == 1
    with session_factory() as s:
        slugs = {left.slug for left in s.execute(select(MarketplaceListing)).scalars().all()}
        assert slugs == {"alpha"}


def test_sync_malformed_index_records_error(
    service: MarketplaceService, registry, session_factory
) -> None:
    class BoomGateway:
        def fetch_index(self, reg):
            from forge_marketplace.errors import RegistryFetchError

            raise RegistryFetchError("schema violation")

        def fetch_manifest(self, reg, uri):  # pragma: no cover
            raise AssertionError

    service._gateway = BoomGateway()  # type: ignore[attr-defined]
    report = service.sync_registry(workspace_id=WS_ID, actor="user:x", registry_id=registry.id)
    assert report.status == "error"
    with session_factory() as s:
        reg = s.get(type(registry), registry.id)
        assert reg.last_sync_status == "error"
        assert s.execute(select(MarketplaceListing)).scalars().all() == []


def test_browse_and_search(
    admin_client: TestClient,
    service: MarketplaceService,
    registry,
    gateway: FakeGateway,
    keypair: Keypair,
) -> None:
    m1, v1 = make_skill_version(keypair, slug="searchable", name="Findable Widget")
    gateway.add(m1, v1)
    service.sync_registry(workspace_id=WS_ID, actor="user:x", registry_id=registry.id)

    resp = admin_client.get("/marketplace/listings")
    assert resp.status_code == 200
    assert any(x["slug"] == "searchable" for x in resp.json())
    # free-text search
    resp2 = admin_client.get("/marketplace/listings", params={"q": "findable"})
    assert any(x["slug"] == "searchable" for x in resp2.json())
    # detail with versions
    resp3 = admin_client.get(f"/marketplace/listings/{registry.slug}/searchable")
    assert resp3.status_code == 200
    assert resp3.json()["versions"][0]["version"] == "1.2.0"


def test_update_flow_and_pin(
    admin_client: TestClient,
    service: MarketplaceService,
    registry,
    gateway: FakeGateway,
    keypair: Keypair,
    session_factory,
) -> None:
    m1, v1 = make_skill_version(keypair, slug="upd", name="upd", version="1.0.0")
    gateway.add(m1, v1)
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    inst_resp = admin_client.post(
        "/marketplace/install",
        json={"registry_id": str(registry.id), "kind": "skill_profile", "slug": "upd"},
    )
    assert inst_resp.status_code == 200, inst_resp.text
    installation_id = inst_resp.json()["installation_id"]

    # New version appears + resync + refresh flags.
    m2, v2 = make_skill_version(
        keypair,
        slug="upd",
        name="upd",
        version="1.3.0",
        artifact={"name": "upd", "min_test_coverage": 99},
    )
    gateway.add(m2, v2)
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    service.refresh_update_flags(workspace_id=WS_ID)

    with session_factory() as s:
        inst = s.get(MarketplaceInstallation, uuid.UUID(installation_id))
        assert inst.status == "update_available"
        assert inst.available_version == "1.3.0"

    # Apply the update -> body replaced, version bumped.
    upd_resp = admin_client.post(f"/marketplace/installations/{installation_id}/update")
    assert upd_resp.status_code == 200, upd_resp.text
    with session_factory() as s:
        inst = s.get(MarketplaceInstallation, uuid.UUID(installation_id))
        assert inst.installed_version == "1.3.0"
        assert inst.status == "installed"
        profile = s.get(SkillProfile, inst.target_object_id)
        assert profile.behavior["min_test_coverage"] == 99


def test_pinned_not_flagged(
    service: MarketplaceService, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    m1, v1 = make_skill_version(keypair, slug="pinme", name="pinme", version="1.0.0")
    gateway.add(m1, v1)
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    result = service.install(
        workspace_id=WS_ID,
        actor="u",
        actor_user_id=None,
        request=_req(registry, "skill_profile", "pinme"),
    )
    service.set_pin(workspace_id=WS_ID, installation_id=result.installation_id, pinned=True)
    m2, v2 = make_skill_version(keypair, slug="pinme", name="pinme", version="2.0.0")
    gateway.add(m2, v2)
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    service.refresh_update_flags(workspace_id=WS_ID)
    with session_factory() as s:
        inst = s.get(MarketplaceInstallation, result.installation_id)
        assert inst.status == "installed"  # pinned -> never flips to update_available


def test_yank_propagation(
    service: MarketplaceService, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    m1, v1 = make_skill_version(keypair, slug="yankable", name="yankable", version="1.0.0")
    gateway.add(m1, v1)
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    result = service.install(
        workspace_id=WS_ID,
        actor="u",
        actor_user_id=None,
        request=_req(registry, "skill_profile", "yankable"),
    )
    # Yank the installed version upstream.
    v1.yanked = True
    v1.yanked_reason = "CVE-2026-0001"
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    service.refresh_update_flags(workspace_id=WS_ID)
    with session_factory() as s:
        inst = s.get(MarketplaceInstallation, result.installation_id)
        assert inst.yanked_reason == "CVE-2026-0001"


def test_uninstall_deletes_object(
    admin_client: TestClient,
    service: MarketplaceService,
    registry,
    gateway: FakeGateway,
    keypair: Keypair,
    session_factory,
) -> None:
    m1, v1 = make_mcp_version(keypair, slug="rmconn", name="rmconn")
    gateway.add(m1, v1)
    result = service.install(
        workspace_id=WS_ID,
        actor="u",
        actor_user_id=None,
        request=_req(registry, "mcp_connector", "rmconn"),
    )
    conn_id = result.target_object_id
    resp = admin_client.delete(f"/marketplace/installations/{result.installation_id}")
    assert resp.status_code == 204
    with session_factory() as s:
        assert s.get(MCPConnection, conn_id) is None
        inst = s.get(MarketplaceInstallation, result.installation_id)
        assert inst.status == "uninstalled"
        assert inst.target_object_id is None


def _req(registry, kind: str, slug: str):
    from forge_marketplace.models import ArtifactKind, InstallRequest

    return InstallRequest(registry_id=registry.id, kind=ArtifactKind(kind), slug=slug)
