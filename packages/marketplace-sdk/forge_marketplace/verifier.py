"""The trust boundary: content-hash + Ed25519 signature verification.

:func:`verify_version` implements the verification precedence (F32 §3.2):

1. ``content_hash``: ``sha256(artifact_bytes)`` must equal the version's
   ``content_hash`` -> else :data:`~VerificationStatus.hash_mismatch` (hard block).
2. if a signature *and* a registry public key exist: detached Ed25519 verify over
   the ``manifest_hash`` bytes -> ``verified`` | ``signature_invalid`` (block).
3. signature present but no registry key -> ``untrusted_registry`` (soft gate).
4. no signature -> ``unsigned`` (soft gate).

The function **never raises** — it always returns a :class:`VerificationResult`.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from forge_marketplace.models import (
    RegistryIndexVersion,
    VerificationResult,
    VerificationStatus,
)


class Ed25519SignatureVerifier:
    """Pure detached-Ed25519 verifier (implements the ``SignatureVerifier`` Protocol)."""

    def verify(self, *, payload: bytes, signature_b64: str, public_key_b64: str) -> bool:
        """Return ``True`` iff ``signature_b64`` is a valid Ed25519 sig over ``payload``.

        Pure and total: any decode/verify failure returns ``False`` (never raises).
        """
        try:
            key_bytes = base64.b64decode(public_key_b64, validate=True)
            sig_bytes = base64.b64decode(signature_b64, validate=True)
            public_key = Ed25519PublicKey.from_public_bytes(key_bytes)
            public_key.verify(sig_bytes, payload)
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False


def _manifest_hash_bytes(manifest_hash: str) -> bytes:
    """The bytes a signature covers: the ``sha256:<hex>`` manifest_hash, UTF-8."""
    return manifest_hash.encode("utf-8")


def verify_version(
    *,
    artifact_bytes: bytes,
    version: RegistryIndexVersion,
    registry_public_key: str | None,
    verifier: Ed25519SignatureVerifier | None = None,
) -> VerificationResult:
    """Verify a fetched artifact against a registry index version. Never raises."""
    import hashlib

    verifier = verifier or Ed25519SignatureVerifier()

    computed = f"sha256:{hashlib.sha256(artifact_bytes).hexdigest()}"
    if computed != version.content_hash:
        return VerificationResult(
            status=VerificationStatus.hash_mismatch,
            content_hash_ok=False,
            signature_ok=None,
            detail=f"content hash mismatch (computed={computed}, expected={version.content_hash})",
        )

    if version.signature and registry_public_key:
        ok = verifier.verify(
            payload=_manifest_hash_bytes(version.manifest_hash),
            signature_b64=version.signature,
            public_key_b64=registry_public_key,
        )
        if ok:
            return VerificationResult(
                status=VerificationStatus.verified,
                content_hash_ok=True,
                signature_ok=True,
                detail="content hash and Ed25519 signature verified",
            )
        return VerificationResult(
            status=VerificationStatus.signature_invalid,
            content_hash_ok=True,
            signature_ok=False,
            detail="detached Ed25519 signature failed verification",
        )

    if version.signature and not registry_public_key:
        return VerificationResult(
            status=VerificationStatus.untrusted_registry,
            content_hash_ok=True,
            signature_ok=None,
            detail="version is signed but the registry has no trusted public key",
        )

    return VerificationResult(
        status=VerificationStatus.unsigned,
        content_hash_ok=True,
        signature_ok=None,
        detail="version carries no signature",
    )


__all__ = ["Ed25519SignatureVerifier", "verify_version"]
