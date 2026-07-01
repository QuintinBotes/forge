"""Marketplace worker task tests (F32 AC2/AC3/AC13/AC15).

Uses in-memory SQLite + a pure ``FakeGateway`` (no Celery broker, no network),
mirroring the deterministic-core testing pattern of the other worker tasks.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import StaticPool, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.marketplace_service import (
    MarketplaceService,
    seed_official_registry,
)
from forge_db.base import Base
from forge_db.models import User, Workspace
from forge_db.models.marketplace import (
    MarketplaceInstallation,
    MarketplaceListing,
    MarketplaceRegistry,
)
from forge_marketplace.manifest import compute_manifest_hash
from forge_marketplace.models import (
    ArtifactKind,
    InstallRequest,
    RegistryIndex,
    RegistryIndexEntry,
    RegistryIndexVersion,
)
from forge_marketplace.packaging import build_package
from forge_worker.tasks.marketplace import (
    refresh_update_flags_core,
    sync_all_core,
    sync_registry_core,
)

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    return priv, pub


def _skill_version(priv, *, slug, version="1.0.0", yanked=False):
    manifest = build_package(
        kind=ArtifactKind.skill_profile,
        artifact={"name": slug, "min_test_coverage": 90},
        slug=slug,
        name=slug,
        version=version,
        summary="s",
        validate_artifact=False,
    )
    mh = compute_manifest_hash(manifest)
    v = RegistryIndexVersion(
        version=version,
        content_hash=manifest.content_hash,
        manifest_hash=mh,
        signature=base64.b64encode(priv.sign(mh.encode())).decode(),
        manifest_uri=f"skill_profile/{slug}/{version}/forge-package.yaml",
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        yanked=yanked,
        yanked_reason="cve" if yanked else None,
    )
    return manifest, v


class FakeGateway:
    def __init__(self, public_key: str | None):
        self.public_key = public_key
        self._pkgs: dict[tuple, list] = {}
        self._manifests: dict[str, tuple] = {}

    def add(self, manifest, version):
        self._pkgs.setdefault((manifest.kind, manifest.slug), []).append((manifest, version))
        self._manifests[version.manifest_uri] = (manifest, b"raw")

    def fetch_index(self, registry) -> RegistryIndex:
        entries = []
        for (kind, slug), items in self._pkgs.items():
            versions = [v for _, v in items]
            m0 = items[-1][0]
            entries.append(
                RegistryIndexEntry(
                    kind=kind, slug=slug, name=m0.name, summary=m0.summary,
                    latest_version=sorted(v.version for v in versions)[-1], versions=versions,
                )
            )
        return RegistryIndex(
            registry_name="R", public_key=self.public_key,
            generated_at=datetime.now(UTC), entries=entries,
        )

    def fetch_manifest(self, registry, uri):
        return self._manifests[uri]


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Workspace(id=WS_ID, name="W", slug="w"))
        s.flush()
        s.add(User(id=uuid.uuid4(), workspace_id=WS_ID, email="a@w.test", name="A", role="admin"))
        s.commit()
    return factory


def _registry(session_factory, pub) -> MarketplaceRegistry:
    with session_factory() as s:
        reg = MarketplaceRegistry(
            workspace_id=WS_ID, slug="r", name="R", type="http_index",
            url="https://r.test/index.json", public_key=pub, trust_level="community", enabled=True,
        )
        s.add(reg)
        s.commit()
        s.refresh(reg)
        s.expunge(reg)
        return reg


def test_sync_registry_upserts_and_audits(session_factory) -> None:
    priv, pub = _keypair()
    gw = FakeGateway(pub)
    m, v = _skill_version(priv, slug="alpha")
    gw.add(m, v)
    reg = _registry(session_factory, pub)
    service = MarketplaceService(session_factory=session_factory, gateway=gw)

    report = sync_registry_core(service, reg.id)
    assert report["status"] == "ok"
    assert report["listings_upserted"] == 1
    with session_factory() as s:
        assert s.execute(select(func.count()).select_from(MarketplaceListing)).scalar_one() == 1


def test_sync_all_registries(session_factory) -> None:
    priv, pub = _keypair()
    gw = FakeGateway(pub)
    gw.add(*_skill_version(priv, slug="pkg-a"))
    _registry(session_factory, pub)
    service = MarketplaceService(session_factory=session_factory, gateway=gw)
    assert sync_all_core(service) == 1


def test_refresh_update_flags_and_yank(session_factory) -> None:
    priv, pub = _keypair()
    gw = FakeGateway(pub)
    m1, v1 = _skill_version(priv, slug="pkg-p", version="1.0.0")
    gw.add(m1, v1)
    reg = _registry(session_factory, pub)
    service = MarketplaceService(session_factory=session_factory, gateway=gw)
    sync_registry_core(service, reg.id)
    result = service.install(
        workspace_id=WS_ID, actor="u", actor_user_id=None,
        request=InstallRequest(registry_id=reg.id, kind=ArtifactKind.skill_profile, slug="pkg-p"),
    )

    # newer version -> update_available
    gw.add(*_skill_version(priv, slug="pkg-p", version="2.0.0"))
    sync_registry_core(service, reg.id)
    flagged = refresh_update_flags_core(service)
    assert flagged == 1
    with session_factory() as s:
        inst = s.get(MarketplaceInstallation, result.installation_id)
        assert inst.status == "update_available"
        assert inst.available_version == "2.0.0"


def test_official_registry_seeded(session_factory) -> None:
    """AC2: seeding creates a read-only official registry row per workspace."""
    with session_factory() as s:
        reg = seed_official_registry(
            s, workspace_id=WS_ID, url="https://official/index.json", public_key="k"
        )
        s.commit()
        assert reg.slug == "official"
        assert reg.trust_level == "official"
        # idempotent
        again = seed_official_registry(
            s, workspace_id=WS_ID, url="https://official/index.json", public_key="k"
        )
        s.commit()
        assert again.id == reg.id
        count = s.execute(
            select(func.count()).select_from(MarketplaceRegistry).where(
                MarketplaceRegistry.slug == "official"
            )
        ).scalar_one()
        assert count == 1
