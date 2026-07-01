"""Shared pure test builders for the marketplace SDK (no network, no DB).

Uniquely named (not ``conftest``) so it imports cleanly under pytest's prepend
import mode during the whole-repo suite without colliding with other packages'
conftests. Also reused by the API/worker tests.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from forge_marketplace.manifest import canonical_artifact_bytes, compute_manifest_hash
from forge_marketplace.models import ArtifactKind, PackageManifest, RegistryIndexVersion
from forge_marketplace.packaging import build_package


@dataclass
class Keypair:
    private: Ed25519PrivateKey
    public_b64: str

    def sign(self, manifest_hash: str) -> str:
        sig = self.private.sign(manifest_hash.encode("utf-8"))
        return base64.b64encode(sig).decode("ascii")


def generate_keypair() -> Keypair:
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes_raw()
    return Keypair(private=priv, public_b64=base64.b64encode(pub_raw).decode("ascii"))


@dataclass
class Package:
    manifest: PackageManifest
    version: RegistryIndexVersion
    artifact_bytes: bytes


def _make_version(
    manifest: PackageManifest,
    *,
    keypair: Keypair | None,
    sign: bool,
    yanked: bool,
    min_forge_version: str | None,
    content_hash_override: str | None,
    signature_override: str | None,
) -> RegistryIndexVersion:
    manifest_hash = compute_manifest_hash(manifest)
    signature: str | None = None
    if sign and keypair is not None:
        signature = keypair.sign(manifest_hash)
    if signature_override is not None:
        signature = signature_override
    return RegistryIndexVersion(
        version=manifest.version,
        content_hash=content_hash_override or manifest.content_hash,
        manifest_hash=manifest_hash,
        signature=signature,
        manifest_uri=f"{manifest.kind.value}/{manifest.slug}/{manifest.version}/forge-package.yaml",
        min_forge_version=min_forge_version,
        published_at=datetime(2026, 6, 20, tzinfo=UTC),
        yanked=yanked,
        yanked_reason="security issue" if yanked else None,
    )


def make_skill_package(
    *,
    keypair: Keypair,
    slug: str = "backend-tdd-strict",
    name: str = "backend-tdd-strict",
    version: str = "1.2.0",
    sign: bool = True,
    yanked: bool = False,
    min_forge_version: str | None = None,
    artifact_override: dict[str, Any] | None = None,
    content_hash_override: str | None = None,
    signature_override: str | None = None,
) -> Package:
    artifact = artifact_override or {
        "schema_version": 1,
        "name": name,
        "description": "Backend feature development with strict TDD discipline",
        "requires_plan": True,
        "requires_tests_before_implementation": True,
        "min_test_coverage": 95,
        "verification_steps": ["lint", "type_check", "unit_tests"],
        "review_required": True,
        "forbidden_shortcuts": ["skip_tests"],
    }
    manifest = build_package(
        kind=ArtifactKind.skill_profile,
        artifact=artifact,
        slug=slug,
        name=name,
        version=version,
        summary="Hardened backend-tdd profile",
        min_forge_version=min_forge_version,
        validate_artifact=False,
    )
    return Package(
        manifest=manifest,
        version=_make_version(
            manifest,
            keypair=keypair,
            sign=sign,
            yanked=yanked,
            min_forge_version=min_forge_version,
            content_hash_override=content_hash_override,
            signature_override=signature_override,
        ),
        artifact_bytes=canonical_artifact_bytes(artifact),
    )


def make_mcp_package(
    *,
    keypair: Keypair,
    slug: str = "confluence-readonly",
    name: str = "Confluence (read-only)",
    version: str = "2.0.0",
    sign: bool = True,
    transport: str = "http",
    allow_write: bool = False,
    allowed_namespaces: list[str] | None = None,
    min_forge_version: str | None = None,
    artifact_override: dict[str, Any] | None = None,
) -> Package:
    artifact = artifact_override or {
        "id": slug,
        "name": name,
        "transport": transport,
        "endpoint": "https://mcp.example.com/confluence",
        "allow_write": allow_write,
        "allowed_namespaces": (
            allowed_namespaces if allowed_namespaces is not None else ["confluence"]
        ),
    }
    manifest = build_package(
        kind=ArtifactKind.mcp_connector,
        artifact=artifact,
        slug=slug,
        name=name,
        version=version,
        summary="Read-only Confluence MCP connector",
        min_forge_version=min_forge_version,
        validate_artifact=False,
    )
    return Package(
        manifest=manifest,
        version=_make_version(
            manifest,
            keypair=keypair,
            sign=sign,
            yanked=False,
            min_forge_version=min_forge_version,
            content_hash_override=None,
            signature_override=None,
        ),
        artifact_bytes=canonical_artifact_bytes(artifact),
    )
