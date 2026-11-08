"""Spec K §4.4 kind policy, wired into install and update.

``workflow_template`` installs and updates only with a ``verified`` signature,
at every registry trust level, and never from a yanked version.
``acknowledge_unverified`` never helps, including the update path's forced one.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import (
    LINE_SLUG,
    WS_ID,
    FakeGateway,
    Keypair,
    count_rows,
    make_line_version,
    make_skill_version,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from forge_api.services.marketplace_service import MarketplaceService
from forge_db.models.marketplace import (
    MarketplaceAuditLog,
    MarketplaceInstallation,
    MarketplaceRegistry,
)
from forge_db.models.workflow_editor import WorkflowDefinition
from forge_marketplace.models import ArtifactKind, InstallRequest

SLUG = LINE_SLUG
_wt_version = make_line_version


def _registry(session_factory, *, trust_level: str, public_key: str | None):
    with session_factory() as s:
        reg = MarketplaceRegistry(
            workspace_id=WS_ID,
            slug=f"reg-{trust_level}",
            name=trust_level,
            type="http_index",
            url=f"https://{trust_level}.test/index.json",
            public_key=public_key,
            trust_level=trust_level,
            enabled=True,
        )
        s.add(reg)
        s.commit()
        s.refresh(reg)
        s.expunge(reg)
        return reg


def _denied(
    session_factory, error_code: str, operation: str = "install"
) -> list[MarketplaceAuditLog]:
    with session_factory() as s:
        return list(
            s.execute(
                select(MarketplaceAuditLog).where(
                    MarketplaceAuditLog.result_status == "denied",
                    MarketplaceAuditLog.error_code == error_code,
                    MarketplaceAuditLog.listing_slug == SLUG,
                    MarketplaceAuditLog.operation == operation,
                )
            )
            .scalars()
            .all()
        )


def _install(client: TestClient, registry, *, version: str | None = None):
    return client.post(
        "/marketplace/install",
        json={
            "registry_id": str(registry.id),
            "kind": "workflow_template",
            "slug": SLUG,
            "version": version,
            "acknowledge_unverified": True,  # never helps for this kind
        },
    )


@pytest.mark.parametrize("trust_level", ["official", "trusted", "community", "unverified"])
@pytest.mark.parametrize(
    ("signed", "registry_has_key"),
    [(False, True), (True, False)],
    ids=["unsigned", "untrusted_registry"],
)
def test_unverified_install_refused_at_every_trust_level(
    admin_client: TestClient,
    gateway: FakeGateway,
    keypair: Keypair,
    session_factory,
    trust_level: str,
    signed: bool,
    registry_has_key: bool,
) -> None:
    reg = _registry(
        session_factory,
        trust_level=trust_level,
        public_key=keypair.public_b64 if registry_has_key else None,
    )
    gateway.add(*_wt_version(keypair, sign=signed))

    resp = _install(admin_client, reg)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "signature_required"
    assert _denied(session_factory, "signature_required")
    assert count_rows(session_factory, MarketplaceInstallation) == 0


def test_yanked_verified_install_refused(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair, session_factory
) -> None:
    gateway.add(*_wt_version(keypair, yanked=True))

    resp = _install(admin_client, registry, version="1.0.0")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "version_yanked"
    assert _denied(session_factory, "version_yanked")
    assert count_rows(session_factory, MarketplaceInstallation) == 0


@pytest.mark.parametrize("trust_level", ["official", "unverified"])
def test_verified_install_passes_the_policy(
    service: MarketplaceService,
    gateway: FakeGateway,
    keypair: Keypair,
    session_factory,
    trust_level: str,
) -> None:
    reg = _registry(session_factory, trust_level=trust_level, public_key=keypair.public_b64)
    gateway.add(*_wt_version(keypair))

    result = service.install(
        workspace_id=WS_ID,
        actor="admin",
        actor_user_id=None,
        request=InstallRequest(registry_id=reg.id, kind=ArtifactKind.workflow_template, slug=SLUG),
    )
    assert (result.target_kind, result.status.value) == ("workflow_definition", "installed")
    assert not _denied(session_factory, "signature_required")
    with session_factory() as s:
        assert s.get(WorkflowDefinition, result.target_object_id) is not None


def _seed_installation(session_factory, registry, keypair: Keypair) -> uuid.UUID:
    manifest, _ = _wt_version(keypair)
    with session_factory() as s:
        row = MarketplaceInstallation(
            workspace_id=WS_ID,
            registry_slug=registry.slug,
            listing_slug=SLUG,
            kind=ArtifactKind.workflow_template.value,
            installed_version="1.0.0",
            target_kind="workflow_definition",
            target_object_id=uuid.uuid4(),
            content_hash=manifest.content_hash,
            verification_status="verified",
            status="installed",
        )
        s.add(row)
        s.commit()
        return row.id


@pytest.mark.parametrize(
    ("sign", "yanked", "code"),
    [(False, False, "signature_required"), (True, True, "version_yanked")],
)
def test_update_refused_despite_forced_acknowledgement(
    admin_client: TestClient,
    registry,
    gateway: FakeGateway,
    keypair: Keypair,
    session_factory,
    sign: bool,
    yanked: bool,
    code: str,
) -> None:
    installation_id = _seed_installation(session_factory, registry, keypair)
    gateway.add(*_wt_version(keypair, version="1.1.0", sign=sign, yanked=yanked))

    resp = admin_client.post(f"/marketplace/installations/{installation_id}/update?version=1.1.0")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == code
    assert _denied(session_factory, code, "update")
    with session_factory() as s:
        row = s.get(MarketplaceInstallation, installation_id)
        assert row is not None
        assert row.installed_version == "1.0.0"


def test_verified_update_passes_the_policy(
    service: MarketplaceService, registry, gateway: FakeGateway, keypair: Keypair
) -> None:
    gateway.add(*_wt_version(keypair))
    installed = service.install(
        workspace_id=WS_ID,
        actor="admin",
        actor_user_id=None,
        request=InstallRequest(
            registry_id=registry.id, kind=ArtifactKind.workflow_template, slug=SLUG
        ),
    )
    gateway.add(*_wt_version(keypair, version="1.1.0"))

    result = service.update_installation(
        workspace_id=WS_ID,
        actor="admin",
        installation_id=installed.installation_id,
        version="1.1.0",
    )
    assert (result.version, result.target_object_id) == ("1.1.0", installed.target_object_id)


def test_yanked_skill_still_installs_with_a_warning(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair
) -> None:
    """Other kinds keep today's behaviour: a yanked version is a warning, not a block."""
    gateway.add(*make_skill_version(keypair, slug="old-skill", name="old-skill", yanked=True))
    resp = admin_client.post(
        "/marketplace/install",
        json={
            "registry_id": str(registry.id),
            "kind": "skill_profile",
            "slug": "old-skill",
            "version": "1.2.0",
        },
    )
    assert resp.status_code == 200, resp.text
    assert any(w.startswith("Yanked:") for w in resp.json()["warnings"])
