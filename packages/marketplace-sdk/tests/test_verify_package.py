"""``verify_package``: the fetched manifest is bound to the signed hash + identity.

Spec K §4.4 — a genuine signature must not sit beside swapped content: the
manifest's kind/slug/version must match the index entry, the index version and
the install request, ``compute_manifest_hash(manifest)`` must equal the signed
``manifest_hash``, and the content hashes must agree — all hard blocks
(``manifest_mismatch``) before signature verification even runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from _mp_helpers import Keypair, Package

from forge_marketplace.manifest import compute_manifest_hash
from forge_marketplace.models import (
    ArtifactKind,
    InstallRequest,
    RegistryIndexEntry,
    RegistryIndexVersion,
    VerificationStatus,
)
from forge_marketplace.verifier import verify_package

REGISTRY_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def _entry(pkg: Package) -> RegistryIndexEntry:
    return RegistryIndexEntry(
        kind=pkg.manifest.kind,
        slug=pkg.manifest.slug,
        name=pkg.manifest.name,
        summary=pkg.manifest.summary,
        latest_version=pkg.manifest.version,
        versions=[pkg.version],
    )


def _request(pkg: Package, **over: object) -> InstallRequest:
    body: dict = {
        "registry_id": REGISTRY_ID,
        "kind": pkg.manifest.kind,
        "slug": pkg.manifest.slug,
        "version": pkg.manifest.version,
    }
    body.update(over)
    return InstallRequest.model_validate(body)


def test_verify_package_genuine_package_verifies(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_skill_package(sign=True)
    result = verify_package(
        pkg.manifest, _entry(pkg), pkg.version, _request(pkg), signing_keypair.public_b64
    )
    assert result.status is VerificationStatus.verified
    assert result.blocked is False


def test_verify_package_tampered_manifest_beside_genuine_signature_blocks(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    """The index keeps the genuine (signed) manifest_hash but the fetched
    manifest was swapped and the index content_hash re-pointed at it."""
    genuine = make_skill_package(sign=True)
    tampered = make_skill_package(
        sign=True, artifact_override={"name": "backend-tdd-strict", "min_test_coverage": 0}
    )
    swapped_version = RegistryIndexVersion(
        version=genuine.version.version,
        content_hash=tampered.manifest.content_hash,  # re-pointed at the swap
        manifest_hash=genuine.version.manifest_hash,  # still the signed hash
        signature=genuine.version.signature,  # still a genuine signature
        manifest_uri=genuine.version.manifest_uri,
        published_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    result = verify_package(
        tampered.manifest,
        _entry(genuine),
        swapped_version,
        _request(genuine),
        signing_keypair.public_b64,
    )
    assert result.status is VerificationStatus.manifest_mismatch
    assert result.blocked is True
    assert compute_manifest_hash(tampered.manifest) != genuine.version.manifest_hash


def test_verify_package_kind_mismatch_blocks(
    make_skill_package: Callable[..., Package],
    make_mcp_package: Callable[..., Package],
    signing_keypair: Keypair,
) -> None:
    skill = make_skill_package(sign=True)
    mcp = make_mcp_package(sign=True)
    # A skill manifest served for an mcp_connector install request.
    result = verify_package(
        skill.manifest,
        _entry(mcp),
        mcp.version,
        _request(mcp),
        signing_keypair.public_b64,
    )
    assert result.status is VerificationStatus.manifest_mismatch
    assert result.blocked is True


def test_verify_package_slug_mismatch_blocks(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_skill_package(sign=True)
    result = verify_package(
        pkg.manifest,
        _entry(pkg),
        pkg.version,
        _request(pkg, slug="a-different-slug"),
        signing_keypair.public_b64,
    )
    assert result.status is VerificationStatus.manifest_mismatch
    assert result.blocked is True


def test_verify_package_version_mismatch_blocks(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_skill_package(sign=True)
    other_version = pkg.version.model_copy(update={"version": "9.9.9"})
    result = verify_package(
        pkg.manifest, _entry(pkg), other_version, _request(pkg), signing_keypair.public_b64
    )
    assert result.status is VerificationStatus.manifest_mismatch
    assert result.blocked is True


def test_verify_package_content_hash_mismatch_blocks(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_skill_package(sign=True, content_hash_override="sha256:" + "0" * 64)
    result = verify_package(
        pkg.manifest, _entry(pkg), pkg.version, _request(pkg), signing_keypair.public_b64
    )
    assert result.status is VerificationStatus.manifest_mismatch
    assert result.blocked is True


def test_verify_package_unsigned_still_soft_gated_after_identity_checks(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_skill_package(sign=False)
    result = verify_package(
        pkg.manifest, _entry(pkg), pkg.version, _request(pkg), signing_keypair.public_b64
    )
    assert result.status is VerificationStatus.unsigned
    assert result.needs_acknowledgement is True
    assert result.blocked is False


def test_verify_package_covers_every_kind(
    make_mcp_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_mcp_package(sign=True)
    assert pkg.manifest.kind is ArtifactKind.mcp_connector
    result = verify_package(
        pkg.manifest, _entry(pkg), pkg.version, _request(pkg), signing_keypair.public_b64
    )
    assert result.status is VerificationStatus.verified
