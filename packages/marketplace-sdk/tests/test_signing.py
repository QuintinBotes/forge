"""``sign_manifest_hash`` round-trips through the verifier; key files load safely."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from _mp_helpers import Keypair, Package
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from forge_marketplace.manifest import compute_manifest_hash
from forge_marketplace.models import (
    InstallRequest,
    RegistryIndexEntry,
    VerificationStatus,
)
from forge_marketplace.signing import SigningKeyError, load_private_key, sign_manifest_hash
from forge_marketplace.verifier import Ed25519SignatureVerifier, verify_package, verify_version


def _write_key(path: Path, data: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(data)
    path.chmod(mode)
    return path


def test_signature_verifies_with_the_verifier(signing_keypair: Keypair) -> None:
    manifest_hash = "sha256:" + "ab" * 32
    sig = sign_manifest_hash(manifest_hash, signing_keypair.private)
    verifier = Ed25519SignatureVerifier()
    assert verifier.verify(
        payload=manifest_hash.encode("utf-8"),
        signature_b64=sig,
        public_key_b64=signing_keypair.public_b64,
    )
    other = "sha256:" + "cd" * 32
    assert not verifier.verify(
        payload=other.encode("utf-8"),
        signature_b64=sig,
        public_key_b64=signing_keypair.public_b64,
    )


def test_signature_round_trips_through_verify_version_and_verify_package(
    make_skill_package: Callable[..., Package], signing_keypair: Keypair
) -> None:
    pkg = make_skill_package(sign=False)
    manifest_hash = compute_manifest_hash(pkg.manifest)
    version = pkg.version.model_copy(
        update={"signature": sign_manifest_hash(manifest_hash, signing_keypair.private)}
    )
    entry = RegistryIndexEntry(
        kind=pkg.manifest.kind,
        slug=pkg.manifest.slug,
        name=pkg.manifest.name,
        summary=pkg.manifest.summary,
        latest_version=pkg.manifest.version,
        versions=[version],
    )
    request = InstallRequest(
        registry_id=uuid.uuid4(),
        kind=pkg.manifest.kind,
        slug=pkg.manifest.slug,
        version=pkg.manifest.version,
    )

    by_version = verify_version(
        artifact_bytes=pkg.artifact_bytes,
        version=version,
        registry_public_key=signing_keypair.public_b64,
    )
    assert by_version.status is VerificationStatus.verified
    by_package = verify_package(pkg.manifest, entry, version, request, signing_keypair.public_b64)
    assert by_package.status is VerificationStatus.verified

    # A signature over a different hash does not verify.
    wrong = version.model_copy(
        update={"signature": sign_manifest_hash("sha256:" + "0" * 64, signing_keypair.private)}
    )
    result = verify_version(
        artifact_bytes=pkg.artifact_bytes,
        version=wrong,
        registry_public_key=signing_keypair.public_b64,
    )
    assert result.status is VerificationStatus.signature_invalid


def test_load_private_key_from_pem_and_raw_base64(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    raw_b64 = base64.b64encode(key.private_bytes_raw()) + b"\n"
    public = key.public_key().public_bytes_raw()

    for name, data in (("key.pem", pem), ("key.b64", raw_b64)):
        loaded = load_private_key(_write_key(tmp_path / name, data))
        assert loaded.public_key().public_bytes_raw() == public


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o620])
def test_load_private_key_refuses_group_or_other_access(tmp_path: Path, mode: int) -> None:
    key = Ed25519PrivateKey.generate()
    path = _write_key(tmp_path / "key.b64", base64.b64encode(key.private_bytes_raw()), mode)
    with pytest.raises(SigningKeyError, match="chmod 600"):
        load_private_key(path)


@pytest.mark.parametrize(
    "data",
    [
        b"not base64 !!",
        base64.b64encode(b"x" * 31),
        b"-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----\n",
    ],
)
def test_load_private_key_rejects_bad_material(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(SigningKeyError):
        load_private_key(_write_key(tmp_path / "key", data))


def test_load_private_key_rejects_a_non_ed25519_pem(tmp_path: Path) -> None:
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    with pytest.raises(SigningKeyError, match="not an Ed25519"):
        load_private_key(_write_key(tmp_path / "ec.pem", pem))
