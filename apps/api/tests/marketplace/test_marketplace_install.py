"""Install trust-boundary integration tests (AC4/5/6/7/8/9/10/11/12/14)."""

from __future__ import annotations

from conftest import (
    WS_ID,
    FakeGateway,
    Keypair,
    count_rows,
    make_mcp_version,
    make_skill_version,
    seed_official_registry,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from forge_db.models.connections import MCPConnection
from forge_db.models.marketplace import MarketplaceAuditLog, MarketplaceInstallation
from forge_db.models.profiles import SkillProfile


def _install_body(
    registry, kind: str, slug: str, *, ack: bool = False, override_name: str | None = None
) -> dict:
    body: dict = {
        "registry_id": str(registry.id),
        "kind": kind,
        "slug": slug,
        "acknowledge_unverified": ack,
    }
    if override_name is not None:
        body["override_name"] = override_name
    return body


def test_preview_is_side_effect_free(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    manifest, version = make_skill_version(keypair)
    gateway.add(manifest, version)
    before = (
        count_rows(session_factory, SkillProfile),
        count_rows(session_factory, MarketplaceInstallation),
    )
    resp = admin_client.post(
        "/marketplace/preview", json=_install_body(registry, "skill_profile", manifest.slug)
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["blocked"] is False
    assert plan["verification"]["status"] == "verified"
    after = (
        count_rows(session_factory, SkillProfile),
        count_rows(session_factory, MarketplaceInstallation),
    )
    assert before == after  # no rows created by preview


def test_install_skill_creates_object_and_provenance(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    manifest, version = make_skill_version(keypair, slug="custom-strict", name="custom-strict")
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "custom-strict")
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["target_kind"] == "skill_profile"
    assert result["verification"]["status"] == "verified"

    with session_factory() as s:
        profile = s.execute(
            select(SkillProfile).where(SkillProfile.name == "custom-strict")
        ).scalar_one()
        assert profile.behavior["min_test_coverage"] == 95
        inst = s.execute(select(MarketplaceInstallation)).scalar_one()
        assert inst.registry_slug == registry.slug
        assert inst.listing_slug == "custom-strict"
        assert inst.installed_version == "1.2.0"
        assert inst.content_hash == manifest.content_hash
        assert inst.verification_status == "verified"
        assert inst.target_object_id == profile.id
        # exactly one install audit row
        audits = (
            s.execute(select(MarketplaceAuditLog).where(MarketplaceAuditLog.operation == "install"))
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].result_status == "ok"


def test_install_skill_name_collision_409(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    with session_factory() as s:
        s.add(SkillProfile(workspace_id=WS_ID, name="dupe", behavior={}))
        s.commit()
    manifest, version = make_skill_version(keypair, slug="dupe", name="dupe")
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "dupe")
    )
    assert resp.status_code == 409
    # override_name resolves the collision
    resp2 = admin_client.post(
        "/marketplace/install",
        json=_install_body(registry, "skill_profile", "dupe", override_name="dupe-2"),
    )
    assert resp2.status_code == 200, resp2.text


def test_install_mcp_creates_pending_readonly_connection(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    # declared allow_write=true must be overridden to false at install (AC8)
    manifest, version = make_mcp_version(keypair, allow_write=True)
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "mcp_connector", manifest.slug)
    )
    assert resp.status_code == 200, resp.text
    with session_factory() as s:
        conn = s.execute(select(MCPConnection)).scalar_one()
        assert conn.allow_write is False
        assert conn.is_active is False  # "pending" / never auto-connected


def test_install_mcp_stdio_blocked(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    manifest, version = make_mcp_version(keypair, slug="stdio-conn", transport="stdio")
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "mcp_connector", "stdio-conn")
    )
    assert resp.status_code == 422
    assert count_rows(session_factory, MCPConnection) == 0


def test_install_blocked_on_hash_mismatch(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    manifest, version = make_skill_version(keypair, slug="tampered", name="tampered")
    # Corrupt the declared content_hash so the fetched artifact won't match.
    version.content_hash = "sha256:" + "b" * 64
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "tampered")
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "hash_mismatch"
    assert count_rows(session_factory, SkillProfile) == 0
    # a denied audit row was written
    with session_factory() as s:
        denied = (
            s.execute(
                select(MarketplaceAuditLog).where(MarketplaceAuditLog.result_status == "denied")
            )
            .scalars()
            .all()
        )
        assert any(a.error_code == "hash_mismatch" for a in denied)


def test_install_blocked_on_bad_signature(
    admin_client: TestClient, registry, gateway: FakeGateway, session_factory
) -> None:
    # Sign with a DIFFERENT key than the registry trusts -> signature_invalid.
    other = Keypair()
    manifest, version = make_skill_version(other, slug="badsig", name="badsig")
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "badsig")
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "signature_invalid"


def test_unsigned_requires_acknowledgement(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    manifest, version = make_skill_version(
        keypair, slug="unsigned-x", name="unsigned-x", sign=False
    )
    gateway.add(manifest, version)
    # Without acknowledgement -> 422 signature_required.
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "unsigned-x")
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "signature_required"
    # With acknowledgement -> installs, recording verification_status=unsigned.
    resp2 = admin_client.post(
        "/marketplace/install",
        json=_install_body(registry, "skill_profile", "unsigned-x", ack=True),
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["verification"]["status"] == "unsigned"


def test_official_registry_always_strict(
    admin_client: TestClient, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    """AC6: an unsigned package from an official registry is rejected even with ack."""
    with session_factory() as s:
        official = seed_official_registry(
            s, workspace_id=WS_ID, url="https://official/index.json", public_key=keypair.public_b64
        )
        s.commit()
        s.refresh(official)
        s.expunge(official)
    manifest, version = make_skill_version(
        keypair, slug="off-unsigned", name="off-unsigned", sign=False
    )
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install",
        json=_install_body(official, "skill_profile", "off-unsigned", ack=True),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "signature_required"


def test_forge_version_incompatible_blocked(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair
) -> None:
    manifest, version = make_skill_version(keypair, slug="future", name="future", min_forge="9.9.9")
    gateway.add(manifest, version)
    resp = admin_client.post(
        "/marketplace/install",
        json={
            "registry_id": str(registry.id),
            "kind": "skill_profile",
            "slug": "future",
            "version": "1.2.0",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "forge_version_incompatible"
