"""In-app publish endpoint tests (marketplace-publish slice).

``POST /marketplace/publish`` is the in-app counterpart to the offline ``forge
marketplace package`` CLI step: it runs the submitted artifact through the same
authoritative :func:`~forge_marketplace.packaging.build_package` validation
(never bypassed) and, on success, persists a cached listing/version directly —
covering a successful publish, a schema-invalid rejection, version upsert
semantics, and the RBAC/tenant boundary the rest of the router enforces.
"""

from __future__ import annotations

from collections.abc import Callable

from conftest import WS_ID, count_rows, seed_official_registry
from fastapi.testclient import TestClient
from sqlalchemy import select

from forge_contracts import UserRole
from forge_db.models.marketplace import (
    MarketplaceAuditLog,
    MarketplaceListing,
    MarketplaceListingVersion,
)


def _body(registry, *, slug: str = "self-authored", version: str = "1.0.0", **over) -> dict:
    body = {
        "registry_id": str(registry.id),
        "kind": "skill_profile",
        "slug": slug,
        "name": "Self Authored",
        "version": version,
        "summary": "A workspace-authored skill profile.",
        "artifact": {"name": "self-authored", "description": "does the thing"},
    }
    body.update(over)
    return body


def test_publish_creates_new_listing_and_version(
    admin_client: TestClient, registry, session_factory
) -> None:
    resp = admin_client.post("/marketplace/publish", json=_body(registry))
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["slug"] == "self-authored"
    assert payload["registry_slug"] == registry.slug
    assert payload["latest_version"] == "1.0.0"
    assert [v["version"] for v in payload["versions"]] == ["1.0.0"]
    assert payload["versions"][0]["content_hash"].startswith("sha256:")

    with session_factory() as s:
        listing = s.execute(
            select(MarketplaceListing).where(MarketplaceListing.slug == "self-authored")
        ).scalar_one()
        assert listing.workspace_id == WS_ID
        versions = (
            s.execute(
                select(MarketplaceListingVersion).where(
                    MarketplaceListingVersion.listing_id == listing.id
                )
            )
            .scalars()
            .all()
        )
        assert len(versions) == 1
        audit = (
            s.execute(
                select(MarketplaceAuditLog).where(
                    MarketplaceAuditLog.operation == "listing.publish"
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1
        assert audit[0].result_status == "ok"

    # It shows up in the regular browse catalog too.
    listings_resp = admin_client.get("/marketplace/listings")
    assert any(x["slug"] == "self-authored" for x in listings_resp.json())


def test_publish_rejects_invalid_artifact(
    admin_client: TestClient, registry, session_factory
) -> None:
    """A skill_profile artifact missing the required ``name`` fails F11 validation."""
    resp = admin_client.post(
        "/marketplace/publish",
        json=_body(registry, artifact={"description": "no name field"}),
    )
    assert resp.status_code == 422, resp.text
    assert "failed F11 validation" in resp.json()["detail"]

    assert count_rows(session_factory, MarketplaceListing) == 0
    with session_factory() as s:
        audit = (
            s.execute(
                select(MarketplaceAuditLog).where(
                    MarketplaceAuditLog.operation == "listing.publish"
                )
            )
            .scalars()
            .all()
        )
        assert len(audit) == 1
        assert audit[0].result_status == "denied"
        assert audit[0].error_code == "schema_invalid"


def test_publish_rejects_mcp_connector_stdio_transport(admin_client: TestClient, registry) -> None:
    """The security floor (no stdio) applies at publish time too, not just install."""
    resp = admin_client.post(
        "/marketplace/publish",
        json=_body(
            registry,
            slug="stdio-conn",
            kind="mcp_connector",
            artifact={"name": "stdio-conn", "transport": "stdio"},
        ),
    )
    assert resp.status_code == 422, resp.text
    assert "stdio" in resp.json()["detail"]


def test_publish_new_version_bumps_latest(
    admin_client: TestClient, registry, session_factory
) -> None:
    first = admin_client.post("/marketplace/publish", json=_body(registry, version="1.0.0"))
    assert first.status_code == 201, first.text
    second = admin_client.post("/marketplace/publish", json=_body(registry, version="1.1.0"))
    assert second.status_code == 201, second.text
    assert second.json()["latest_version"] == "1.1.0"
    assert {v["version"] for v in second.json()["versions"]} == {"1.0.0", "1.1.0"}

    detail = admin_client.get(f"/marketplace/listings/{registry.slug}/self-authored")
    assert detail.status_code == 200
    assert detail.json()["latest_version"] == "1.1.0"


def test_publish_rejects_duplicate_version(admin_client: TestClient, registry) -> None:
    first = admin_client.post("/marketplace/publish", json=_body(registry, version="2.0.0"))
    assert first.status_code == 201, first.text
    dup = admin_client.post("/marketplace/publish", json=_body(registry, version="2.0.0"))
    assert dup.status_code == 409, dup.text


def test_publish_rejects_official_registry(admin_client: TestClient, session_factory) -> None:
    with session_factory() as s:
        official = seed_official_registry(
            s, workspace_id=WS_ID, url="https://official.example/index.json", public_key=None
        )
        s.commit()
        official_id = official.id

    resp = admin_client.post(
        "/marketplace/publish",
        json={
            "registry_id": str(official_id),
            "kind": "skill_profile",
            "slug": "sneaky",
            "name": "Sneaky",
            "version": "1.0.0",
            "summary": "should be blocked",
            "artifact": {"name": "sneaky"},
        },
    )
    assert resp.status_code == 409, resp.text


def test_member_can_publish_but_viewer_cannot(
    client_factory: Callable[..., TestClient], registry
) -> None:
    member = client_factory(UserRole.MEMBER)
    resp = member.post("/marketplace/publish", json=_body(registry, slug="member-pub"))
    assert resp.status_code == 201, resp.text

    viewer = client_factory(UserRole.VIEWER)
    resp2 = viewer.post("/marketplace/publish", json=_body(registry, slug="viewer-pub"))
    assert resp2.status_code == 403
