"""Fixtures for the marketplace router/service integration tests (F32).

In-memory SQLite (shared across the app worker thread) seeded with two
workspaces + role users, a :class:`FakeGateway` (no network) that serves a
configurable signed registry index + manifests, and a ``MarketplaceService``
wired to it and injected via dependency override. Role/workspace-parametrized
TestClient builder mirrors the F31 deployments harness.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.marketplace import get_marketplace_service
from forge_api.services.marketplace_service import MarketplaceService, seed_official_registry
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import User, Workspace
from forge_db.models.marketplace import MarketplaceRegistry
from forge_marketplace.manifest import compute_manifest_hash
from forge_marketplace.models import (
    ArtifactKind,
    RegistryIndex,
    RegistryIndexEntry,
    RegistryIndexVersion,
)
from forge_marketplace.packaging import build_package

WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")

_ROLE_USER = {UserRole.ADMIN: ADMIN_ID, UserRole.MEMBER: MEMBER_ID, UserRole.VIEWER: VIEWER_ID}


class Keypair:
    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public_b64 = base64.b64encode(self.private.public_key().public_bytes_raw()).decode()

    def sign(self, manifest_hash: str) -> str:
        return base64.b64encode(self.private.sign(manifest_hash.encode())).decode()


def make_skill_version(
    keypair: Keypair | None,
    *,
    slug: str = "backend-tdd-strict",
    name: str = "backend-tdd-strict",
    version: str = "1.2.0",
    sign: bool = True,
    yanked: bool = False,
    min_forge: str | None = None,
    artifact: dict | None = None,
):
    body = artifact or {
        "name": name,
        "description": "strict tdd",
        "min_test_coverage": 95,
        "verification_steps": ["lint", "unit_tests"],
    }
    manifest = build_package(
        kind=ArtifactKind.skill_profile,
        artifact=body,
        slug=slug,
        name=name,
        version=version,
        summary="hardened backend-tdd",
        min_forge_version=min_forge,
        validate_artifact=False,
    )
    mh = compute_manifest_hash(manifest)
    sig = keypair.sign(mh) if (sign and keypair) else None
    v = RegistryIndexVersion(
        version=version,
        content_hash=manifest.content_hash,
        manifest_hash=mh,
        signature=sig,
        manifest_uri=f"skill_profile/{slug}/{version}/forge-package.yaml",
        min_forge_version=min_forge,
        published_at=datetime(2026, 6, 20, tzinfo=UTC),
        yanked=yanked,
        yanked_reason="security" if yanked else None,
    )
    return manifest, v


def make_mcp_version(
    keypair: Keypair | None,
    *,
    slug: str = "confluence-readonly",
    name: str = "Confluence RO",
    version: str = "2.0.0",
    sign: bool = True,
    transport: str = "http",
    allow_write: bool = False,
    namespaces: list[str] | None = None,
):
    body = {
        "id": slug,
        "name": name,
        "transport": transport,
        "endpoint": "https://mcp.example.com/confluence",
        "allow_write": allow_write,
        "allowed_namespaces": namespaces if namespaces is not None else ["confluence"],
    }
    manifest = build_package(
        kind=ArtifactKind.mcp_connector,
        artifact=body,
        slug=slug,
        name=name,
        version=version,
        summary="read-only confluence",
        validate_artifact=False,
    )
    mh = compute_manifest_hash(manifest)
    sig = keypair.sign(mh) if (sign and keypair) else None
    v = RegistryIndexVersion(
        version=version,
        content_hash=manifest.content_hash,
        manifest_hash=mh,
        signature=sig,
        manifest_uri=f"mcp_connector/{slug}/{version}/forge-package.yaml",
        published_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    return manifest, v


class FakeGateway:
    """In-memory registry gateway — no network. Configurable via :meth:`add`."""

    def __init__(self, *, registry_name: str = "Test Registry", public_key: str | None = None):
        self.registry_name = registry_name
        self.public_key = public_key
        self._packages: dict[tuple, list] = {}
        self._manifests: dict[str, tuple] = {}

    def add(self, manifest, version, *, content_bytes: bytes | None = None) -> None:
        key = (manifest.kind, manifest.slug)
        self._packages.setdefault(key, []).append((manifest, version))
        self._manifests[version.manifest_uri] = (manifest, content_bytes or b"raw")

    def _index(self) -> RegistryIndex:
        entries = []
        for (kind, slug), items in self._packages.items():
            versions = [v for _, v in items]
            manifests = [m for m, _ in items]
            m0 = manifests[-1]
            entries.append(
                RegistryIndexEntry(
                    kind=kind,
                    slug=slug,
                    name=m0.name,
                    summary=m0.summary,
                    tags=list(m0.tags),
                    latest_version=sorted(v.version for v in versions)[-1],
                    versions=versions,
                )
            )
        return RegistryIndex(
            registry_name=self.registry_name,
            public_key=self.public_key,
            generated_at=datetime.now(UTC),
            entries=entries,
        )

    def fetch_index(self, registry: MarketplaceRegistry) -> RegistryIndex:
        return self._index()

    def fetch_manifest(self, registry: MarketplaceRegistry, manifest_uri: str):
        return self._manifests[manifest_uri]


@pytest.fixture
def keypair() -> Keypair:
    return Keypair()


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        s.add(Workspace(id=OTHER_WS_ID, name="Other", slug="other"))
        s.flush()
        for uid, email, role in (
            (ADMIN_ID, "admin@acme.test", "admin"),
            (MEMBER_ID, "member@acme.test", "member"),
            (VIEWER_ID, "viewer@acme.test", "viewer"),
        ):
            s.add(User(id=uid, workspace_id=WS_ID, email=email, name="U", role=role))
        s.commit()
    return factory


@pytest.fixture
def gateway(keypair: Keypair) -> FakeGateway:
    return FakeGateway(public_key=keypair.public_b64)


@pytest.fixture
def registry(session_factory: sessionmaker[Session], keypair: Keypair) -> MarketplaceRegistry:
    """A seeded community registry (workspace WS_ID) trusting ``keypair``."""
    with session_factory() as s:
        reg = MarketplaceRegistry(
            workspace_id=WS_ID,
            slug="acme-community",
            name="Acme Community",
            type="http_index",
            url="https://registry.acme.test/index.json",
            public_key=keypair.public_b64,
            trust_level="community",
            enabled=True,
        )
        s.add(reg)
        s.commit()
        s.refresh(reg)
        s.expunge(reg)
        return reg


@pytest.fixture
def service(session_factory: sessionmaker[Session], gateway: FakeGateway) -> MarketplaceService:
    return MarketplaceService(session_factory=session_factory, gateway=gateway)


@pytest.fixture
def client_factory(
    service: MarketplaceService,
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(
        role: UserRole = UserRole.ADMIN,
        *,
        workspace_id: uuid.UUID = WS_ID,
        authenticated: bool = True,
    ) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_marketplace_service] = lambda: service
        if authenticated:
            principal = Principal(
                user_id=_ROLE_USER.get(role, ADMIN_ID),
                workspace_id=workspace_id,
                role=role,
                email="mp-test@forge.local",
                auth_method="test",
                scopes=["*"],
            )
            app.dependency_overrides[get_current_principal] = lambda: principal
        tc = TestClient(app)
        clients.append(tc)
        return tc

    yield _build
    for tc in clients:
        tc.close()


@pytest.fixture
def admin_client(client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory(UserRole.ADMIN)


def count_rows(session_factory: sessionmaker[Session], model) -> int:
    from sqlalchemy import func, select

    with session_factory() as s:
        return s.execute(select(func.count()).select_from(model)).scalar_one()


__all__ = [
    "ADMIN_ID",
    "OTHER_WS_ID",
    "WS_ID",
    "FakeGateway",
    "Keypair",
    "count_rows",
    "make_mcp_version",
    "make_skill_version",
    "seed_official_registry",
]
