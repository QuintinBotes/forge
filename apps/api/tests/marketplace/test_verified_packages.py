"""Spec K P0 (B): verified packages — manifest bound to its signature, explicit kinds.

* A tampered manifest beside a genuine signature is refused
  (``manifest_mismatch``) at every registry trust level, on install and update.
* Kind/slug/version mismatches against the signed identity are refused.
* Reserved kinds (``workflow_template``/``policy_template``) and unknown kinds
  are 422 with a committed denied audit row; no kind falls through to MCP.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import (
    WS_ID,
    FakeGateway,
    Keypair,
    count_rows,
    make_mcp_version,
    make_skill_version,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from forge_db.models.marketplace import (
    MarketplaceAuditLog,
    MarketplaceInstallation,
    MarketplaceRegistry,
)
from forge_db.models.profiles import SkillProfile
from forge_marketplace.models import ArtifactKind, RegistryIndexVersion


def _install_body(registry, kind: str, slug: str, *, ack: bool = True) -> dict:
    return {
        "registry_id": str(registry.id),
        "kind": kind,
        "slug": slug,
        "acknowledge_unverified": ack,
    }


def _denied(session_factory, error_code: str) -> list[MarketplaceAuditLog]:
    with session_factory() as s:
        rows = (
            s.execute(
                select(MarketplaceAuditLog).where(
                    MarketplaceAuditLog.result_status == "denied",
                    MarketplaceAuditLog.error_code == error_code,
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            s.expunge(r)
        return list(rows)


def _registry(session_factory, keypair: Keypair, *, slug: str, trust_level: str):
    with session_factory() as s:
        reg = MarketplaceRegistry(
            workspace_id=WS_ID,
            slug=slug,
            name=slug,
            type="http_index",
            url=f"https://{slug}.test/index.json",
            public_key=keypair.public_b64,
            trust_level=trust_level,
            enabled=True,
        )
        s.add(reg)
        s.commit()
        s.refresh(reg)
        s.expunge(reg)
        return reg


def _tampered_pair(keypair: Keypair, slug: str):
    """(tampered manifest served by the gateway, index version keeping the
    genuine signed manifest_hash but re-pointing content_hash at the swap)."""
    genuine_manifest, genuine_version = make_skill_version(
        keypair, slug=slug, name=slug, version="1.0.0"
    )
    tampered_manifest, _ = make_skill_version(
        keypair,
        slug=slug,
        name=slug,
        version="1.0.0",
        artifact={"name": slug, "description": "swapped", "min_test_coverage": 0},
    )
    swapped_version = RegistryIndexVersion(
        version=genuine_version.version,
        content_hash=tampered_manifest.content_hash,
        manifest_hash=genuine_version.manifest_hash,
        signature=genuine_version.signature,
        manifest_uri=genuine_version.manifest_uri,
        published_at=genuine_version.published_at,
    )
    assert genuine_manifest.content_hash != tampered_manifest.content_hash
    return tampered_manifest, swapped_version


@pytest.mark.parametrize("trust_level", ["official", "trusted", "community", "unverified"])
def test_tampered_manifest_refused_at_every_trust_level(
    admin_client: TestClient, gateway: FakeGateway, keypair: Keypair, session_factory, trust_level
) -> None:
    reg = _registry(session_factory, keypair, slug=f"reg-{trust_level}", trust_level=trust_level)
    slug = f"tampered-{trust_level}"
    manifest, version = _tampered_pair(keypair, slug)
    gateway.add(manifest, version)

    resp = admin_client.post("/marketplace/install", json=_install_body(reg, "skill_profile", slug))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "manifest_mismatch"
    assert count_rows(session_factory, SkillProfile) == 0
    assert _denied(session_factory, "manifest_mismatch")


def test_tampered_manifest_refused_on_update(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    manifest, version = make_skill_version(keypair, slug="updatable", name="updatable")
    gateway.add(manifest, version)
    installed = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "updatable")
    )
    assert installed.status_code == 200, installed.text
    installation_id = installed.json()["installation_id"]

    # The registry now serves a swapped manifest for the next version, still
    # carrying a genuine signature over the wrong (old) manifest hash.
    tampered_manifest, swapped_version = _tampered_pair(keypair, "updatable")
    swapped_version = swapped_version.model_copy(update={"version": "1.3.0"})
    tampered_manifest = tampered_manifest.model_copy(update={"version": "1.3.0"})
    gateway.add(tampered_manifest, swapped_version)

    resp = admin_client.post(f"/marketplace/installations/{installation_id}/update?version=1.3.0")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "manifest_mismatch"
    with session_factory() as s:
        row = s.get(MarketplaceInstallation, uuid.UUID(installation_id))
        assert row is not None
        assert row.installed_version == "1.2.0"  # unchanged


@pytest.mark.parametrize("kind", ["workflow_template", "policy_template"])
def test_reserved_kind_install_is_422_with_audit_row(
    admin_client: TestClient, registry, session_factory, kind: str
) -> None:
    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, kind, "some-line")
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "unsupported_kind"
    rows = _denied(session_factory, "unsupported_kind")
    assert any(r.listing_slug == "some-line" and r.operation == "install" for r in rows)


@pytest.mark.parametrize("kind", ["workflow_template", "policy_template"])
def test_reserved_kind_publish_is_422_with_audit_row(
    admin_client: TestClient, registry, session_factory, kind: str
) -> None:
    resp = admin_client.post(
        "/marketplace/publish",
        json={
            "registry_id": str(registry.id),
            "kind": kind,
            "slug": "authored-line",
            "name": "Authored Line",
            "version": "1.0.0",
            "summary": "a line",
            "artifact": {"name": "authored-line"},
        },
    )
    assert resp.status_code == 422, resp.text
    assert "unsupported_kind" in resp.json()["detail"]
    rows = _denied(session_factory, "unsupported_kind")
    assert any(r.listing_slug == "authored-line" for r in rows)


def test_unknown_kind_update_is_422_with_audit_row(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    """A stored installation whose kind is outside the enum (legacy/corrupt
    row) is refused on update instead of falling through to the MCP path."""
    manifest, version = make_skill_version(keypair, slug="legacy", name="legacy")
    gateway.add(manifest, version)
    installed = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "legacy")
    )
    assert installed.status_code == 200, installed.text
    installation_id = installed.json()["installation_id"]
    with session_factory() as s:
        row = s.get(MarketplaceInstallation, uuid.UUID(installation_id))
        assert row is not None
        row.kind = "mystery_kind"
        s.commit()

    resp = admin_client.post(f"/marketplace/installations/{installation_id}/update")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "unknown_kind"
    assert _denied(session_factory, "unknown_kind")


def test_unknown_target_kind_uninstall_is_422_with_audit_row(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    manifest, version = make_skill_version(keypair, slug="orphan", name="orphan")
    gateway.add(manifest, version)
    installed = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "skill_profile", "orphan")
    )
    assert installed.status_code == 200, installed.text
    installation_id = installed.json()["installation_id"]
    with session_factory() as s:
        row = s.get(MarketplaceInstallation, uuid.UUID(installation_id))
        assert row is not None
        row.target_kind = "mystery_target"
        s.commit()

    resp = admin_client.delete(f"/marketplace/installations/{installation_id}")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "unknown_kind"
    assert _denied(session_factory, "unknown_kind")
    # The installed object was not deleted through the wrong (MCP) branch.
    assert count_rows(session_factory, SkillProfile) == 1


def test_kind_mismatch_manifest_refused(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    """The gateway serves a skill manifest under an mcp index entry: the
    identity triple (manifest/entry/request) disagrees -> manifest_mismatch,
    not an MCP install of a skill package."""

    skill_manifest, _ = make_skill_version(keypair, slug="shape-shift", name="shape-shift")
    _, mcp_version = make_mcp_version(keypair, slug="shape-shift")
    # Serve the skill manifest at the mcp version's URI under an mcp entry.
    mcp_manifest, _ = make_mcp_version(keypair, slug="shape-shift")
    gateway.add(mcp_manifest, mcp_version)
    gateway._manifests[mcp_version.manifest_uri] = (skill_manifest, b"raw")

    resp = admin_client.post(
        "/marketplace/install", json=_install_body(registry, "mcp_connector", "shape-shift")
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "manifest_mismatch"
    assert ArtifactKind.mcp_connector.value == "mcp_connector"
