"""RBAC + tenant isolation + audit-completeness tests (AC18/AC19)."""

from __future__ import annotations

from collections.abc import Callable

from conftest import (
    OTHER_WS_ID,
    WS_ID,
    FakeGateway,
    Keypair,
    make_skill_version,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from forge_api.services.marketplace_service import MarketplaceService
from forge_contracts import UserRole
from forge_db.models.marketplace import MarketplaceAuditLog


def _body(registry, slug: str = "rbac-x") -> dict:
    return {"registry_id": str(registry.id), "kind": "skill_profile", "slug": slug}


def test_member_can_browse_but_not_install(
    client_factory: Callable[..., TestClient], registry, gateway: FakeGateway, keypair: Keypair
) -> None:
    manifest, version = make_skill_version(keypair, slug="rbac-x", name="rbac-x")
    gateway.add(manifest, version)
    member = client_factory(UserRole.MEMBER)
    assert member.get("/marketplace/listings").status_code == 200
    assert member.get("/marketplace/installations").status_code == 200
    # mutating routes require admin
    assert member.post("/marketplace/install", json=_body(registry)).status_code == 403
    assert (
        member.post(
            "/marketplace/registries",
            json={"name": "x", "type": "http_index", "url": "https://x/i.json"},
        ).status_code
        == 403
    )


def test_viewer_cannot_install(
    client_factory: Callable[..., TestClient], registry, gateway: FakeGateway, keypair: Keypair
) -> None:
    manifest, version = make_skill_version(keypair, slug="rbac-x", name="rbac-x")
    gateway.add(manifest, version)
    viewer = client_factory(UserRole.VIEWER)
    assert viewer.post("/marketplace/install", json=_body(registry)).status_code == 403


def test_unauthenticated_401(client_factory: Callable[..., TestClient], registry) -> None:
    anon = client_factory(authenticated=False)
    assert anon.get("/marketplace/listings").status_code == 401


def test_tenant_isolation_cross_workspace_404(
    client_factory: Callable[..., TestClient], registry, gateway: FakeGateway, keypair: Keypair
) -> None:
    manifest, version = make_skill_version(keypair, slug="rbac-x", name="rbac-x")
    gateway.add(manifest, version)
    # The registry belongs to WS_ID; an admin in OTHER_WS cannot see/install it.
    other = client_factory(UserRole.ADMIN, workspace_id=OTHER_WS_ID)
    resp = other.post("/marketplace/install", json=_body(registry))
    assert resp.status_code == 404


def test_audit_completeness_one_row_per_op(
    service: MarketplaceService, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    """AC18: add/sync/install/uninstall each write exactly one audit row."""
    from forge_marketplace.models import ArtifactKind, InstallRequest

    manifest, version = make_skill_version(keypair, slug="audited", name="audited")
    gateway.add(manifest, version)
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    result = service.install(
        workspace_id=WS_ID,
        actor="u",
        actor_user_id=None,
        request=InstallRequest(
            registry_id=registry.id, kind=ArtifactKind.skill_profile, slug="audited"
        ),
    )
    service.uninstall(workspace_id=WS_ID, actor="u", installation_id=result.installation_id)

    with session_factory() as s:
        by_op = dict(
            s.execute(
                select(MarketplaceAuditLog.operation, func.count())
                .where(MarketplaceAuditLog.workspace_id == WS_ID)
                .group_by(MarketplaceAuditLog.operation)
            ).all()
        )
    assert by_op.get("registry.sync") == 1
    assert by_op.get("install") == 1
    assert by_op.get("uninstall") == 1
