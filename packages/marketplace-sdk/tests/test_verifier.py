"""Verification precedence: hash, signature, unsigned/untrusted (AC4/AC5)."""

from __future__ import annotations

import base64
from collections.abc import Callable

from _mp_helpers import Keypair, Package
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from forge_marketplace.models import VerificationStatus
from forge_marketplace.verifier import Ed25519SignatureVerifier, verify_version


def test_verify_version_valid_signature(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_skill_package(sign=True)
    result = verify_version(
        artifact_bytes=pkg.artifact_bytes,
        version=pkg.version,
        registry_public_key=signing_keypair.public_b64,
    )
    assert result.status is VerificationStatus.verified
    assert result.content_hash_ok is True
    assert result.signature_ok is True
    assert result.blocked is False


def test_verify_version_hash_mismatch_blocks(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    """AC4: a fetched artifact whose sha256 != content_hash hard-blocks."""
    pkg = make_skill_package(sign=True)
    result = verify_version(
        artifact_bytes=b"tampered-bytes",
        version=pkg.version,
        registry_public_key=signing_keypair.public_b64,
    )
    assert result.status is VerificationStatus.hash_mismatch
    assert result.blocked is True


def test_verify_version_invalid_signature_blocks(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    """AC5: a tampered signature -> signature_invalid (hard block)."""
    pkg = make_skill_package(sign=True)
    # Verify against a *different* key -> signature cannot validate.
    wrong = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ).decode()
    result = verify_version(
        artifact_bytes=pkg.artifact_bytes,
        version=pkg.version,
        registry_public_key=wrong,
    )
    assert result.status is VerificationStatus.signature_invalid
    assert result.blocked is True


def test_verify_version_untrusted_registry(
    make_skill_package: Callable[..., Package],
) -> None:
    """AC5: signed version on a registry with no public key -> untrusted_registry."""
    pkg = make_skill_package(sign=True)
    result = verify_version(
        artifact_bytes=pkg.artifact_bytes,
        version=pkg.version,
        registry_public_key=None,
    )
    assert result.status is VerificationStatus.untrusted_registry
    assert result.blocked is False
    assert result.needs_acknowledgement is True


def test_verify_version_unsigned(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    """AC5: an unsigned version -> unsigned (soft gate)."""
    pkg = make_skill_package(sign=False)
    result = verify_version(
        artifact_bytes=pkg.artifact_bytes,
        version=pkg.version,
        registry_public_key=signing_keypair.public_b64,
    )
    assert result.status is VerificationStatus.unsigned
    assert result.needs_acknowledgement is True


def test_signature_verifier_is_total() -> None:
    """The verifier never raises — malformed inputs return False."""
    v = Ed25519SignatureVerifier()
    assert v.verify(payload=b"x", signature_b64="!!!not-base64!!!", public_key_b64="also-bad") \
        is False
