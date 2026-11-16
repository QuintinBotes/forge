"""DSSE signer/verifier for Attested Changesets (F41 slice ``dsse-signer``).

Signs and verifies a :class:`~scf_contracts.attestation.DsseEnvelope` over the
DSSE **PAE** (Pre-Authentication Encoding)::

    PAE(type, body) = "DSSEv1 " + len(type) + " " + type + " " + len(body) + " " + body

PAE binds ``payloadType`` into what gets signed — a signature over the raw
payload alone would let an attacker relabel a payload's type without
invalidating the signature — which is why :func:`pae`, not the bare payload
bytes, is what :class:`DsseSigner`/:class:`DsseVerifier` sign/verify.

Crypto follows :class:`scf_marketplace.verifier.Ed25519SignatureVerifier`
exactly: raw Ed25519 keys, base64 on the wire, verification pure and total
(never raises — malformed input is just "not verified", same as that class).

Key material follows :class:`scf_auth.vault.EnvKeyProvider`'s env-sourced
shape (:data:`SIGNING_KEY_ENV` — a base64 32-byte Ed25519 seed). A key that is
set but malformed fails closed. With no key set, the API and the worker sign
with the deployment's stored key (``scf_api.services.attestation_keys``, #175),
so attestations verify across restarts and processes; this module alone falls
back to a loudly-warned, process-ephemeral key only under the explicit
``FORGE_DEV_INSECURE`` development opt-in, and otherwise refuses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import warnings
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from scf_auth.errors import KeyMaterialError
from scf_contracts.attestation import DsseEnvelope, DsseSignature

__all__ = [
    "SIGNING_KEY_ENV",
    "DsseSigner",
    "DsseVerifier",
    "EnvSigningKeyProvider",
    "KeyPair",
    "SigningKey",
    "dev_ephemeral_key_allowed",
    "pae",
]

#: Env var carrying the base64-encoded 32-byte Ed25519 seed used to sign
#: attestations. Mirrors ``scf_auth.vault.EnvKeyProvider``'s ``FORGE_VAULT_KEYS``.
SIGNING_KEY_ENV = "FORGE_ATTEST_SIGNING_KEY"

_SEED_SIZE = 32
_PAE_PREFIX = b"DSSEv1"


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding of ``(payload_type, payload)``.

    ``PAE(type, body) = "DSSEv1 " + len(type) + " " + type + " " + len(body) + " " + body``
    — the exact bytes an Ed25519 signature is computed/verified over (never the
    raw ``payload`` alone, so relabeling ``payloadType`` invalidates the signature).
    """
    type_bytes = payload_type.encode("utf-8")
    return b"".join(
        [
            _PAE_PREFIX,
            b" ",
            str(len(type_bytes)).encode("ascii"),
            b" ",
            type_bytes,
            b" ",
            str(len(payload)).encode("ascii"),
            b" ",
            payload,
        ]
    )


class SigningKey(Protocol):
    """An Ed25519 attestation key: its private half, public half and key id."""

    @property
    def private_key(self) -> Ed25519PrivateKey: ...

    @property
    def public_key_b64(self) -> str: ...

    @property
    def keyid(self) -> str: ...


def dev_ephemeral_key_allowed(environ: dict[str, str] | None = None) -> bool:
    """Whether ``FORGE_DEV_INSECURE`` opts in to a process-ephemeral signing key."""
    env = environ if environ is not None else dict(os.environ)
    return env.get("FORGE_DEV_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}


class KeyPair:
    """An Ed25519 signing key held in memory.

    ``keyid`` is the sha256 hex digest of the raw public key bytes: a stable,
    content-derived identifier a verifier can use to look up the matching
    public key, rather than trusting a signer-asserted id.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        public_bytes = self._private_key.public_key().public_bytes_raw()
        self._public_key_b64 = base64.b64encode(public_bytes).decode("ascii")
        self._keyid = hashlib.sha256(public_bytes).hexdigest()

    @classmethod
    def from_seed_b64(cls, raw: str, *, source: str = SIGNING_KEY_ENV) -> KeyPair:
        """The key whose 32-byte seed ``raw`` encodes in base64; malformed fails closed."""
        try:
            seed = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise KeyMaterialError(f"malformed {source}: invalid base64") from None
        if len(seed) != _SEED_SIZE:
            raise KeyMaterialError(
                f"{source} must decode to a {_SEED_SIZE}-byte Ed25519 seed, got {len(seed)}"
            )
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def ephemeral(cls) -> KeyPair:
        """A process-ephemeral key, with a loud warning (development only)."""
        warnings.warn(
            "FORGE_DEV_INSECURE is set: signing attestations with a process-ephemeral "
            "Ed25519 key. They will fail verification after a restart or from another "
            "process. NEVER set FORGE_DEV_INSECURE in production.",
            stacklevel=3,
        )
        return cls(Ed25519PrivateKey.generate())

    def seed_b64(self) -> str:
        """The private seed, base64 (for storing it encrypted)."""
        return base64.b64encode(self._private_key.private_bytes_raw()).decode("ascii")

    @property
    def private_key(self) -> Ed25519PrivateKey:
        """The Ed25519 private key material."""
        return self._private_key

    @property
    def public_key_b64(self) -> str:
        """Base64-encoded raw public key bytes (share this with verifiers)."""
        return self._public_key_b64

    @property
    def keyid(self) -> str:
        """sha256 hex digest of the raw public key bytes."""
        return self._keyid


class EnvSigningKeyProvider(KeyPair):
    """Ed25519 signing key sourced from :data:`SIGNING_KEY_ENV`.

    Unset, it is a process-ephemeral key only under the ``FORGE_DEV_INSECURE``
    opt-in; otherwise it refuses, and the caller signs with the deployment's
    stored key instead (``scf_api.services.attestation_keys``).
    """

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = environ if environ is not None else dict(os.environ)
        raw = env.get(SIGNING_KEY_ENV, "").strip()
        if raw:
            key = KeyPair.from_seed_b64(raw)
        elif dev_ephemeral_key_allowed(env):
            key = KeyPair.ephemeral()
        else:
            raise KeyMaterialError(
                f"{SIGNING_KEY_ENV} is not set. The API and the worker sign with the "
                "deployment's stored key; elsewhere set the key, or FORGE_DEV_INSECURE=1 "
                "for a process-ephemeral development key."
            )
        super().__init__(key.private_key)


class DsseSigner:
    """Signs a payload into a :class:`DsseEnvelope` over its PAE encoding."""

    def __init__(self, provider: SigningKey | None = None) -> None:
        self._provider = provider or EnvSigningKeyProvider()

    @property
    def keyid(self) -> str:
        """The signing key's :attr:`KeyPair.keyid`."""
        return self._provider.keyid

    @property
    def public_key_b64(self) -> str:
        """The signing key's :attr:`KeyPair.public_key_b64`."""
        return self._provider.public_key_b64

    def sign(self, *, payload_type: str, payload: bytes) -> DsseEnvelope:
        """Sign ``payload`` (raw bytes) and return a single-signature envelope.

        ``envelope.payload`` is the base64 encoding of ``payload`` (DSSE wire
        shape); the Ed25519 signature itself covers :func:`pae` of the two.
        """
        signature = self._provider.private_key.sign(pae(payload_type, payload))
        return DsseEnvelope(
            payloadType=payload_type,
            payload=base64.b64encode(payload).decode("ascii"),
            signatures=[
                DsseSignature(
                    keyid=self._provider.keyid,
                    sig=base64.b64encode(signature).decode("ascii"),
                )
            ],
        )


class DsseVerifier:
    """Pure detached-Ed25519 verifier over a :class:`DsseEnvelope`'s PAE encoding.

    Mirrors :class:`scf_marketplace.verifier.Ed25519SignatureVerifier`: pure
    and total — any decode/verify failure returns ``False``, never raises.
    """

    def verify(self, envelope: DsseEnvelope, *, public_key_b64: str) -> bool:
        """Return ``True`` iff any of ``envelope.signatures`` verifies for ``public_key_b64``."""
        try:
            payload = base64.b64decode(envelope.payload, validate=True)
            key_bytes = base64.b64decode(public_key_b64, validate=True)
            public_key = Ed25519PublicKey.from_public_bytes(key_bytes)
        except (binascii.Error, ValueError, TypeError):
            return False

        pae_bytes = pae(envelope.payloadType, payload)
        for signature in envelope.signatures:
            try:
                sig_bytes = base64.b64decode(signature.sig, validate=True)
                public_key.verify(sig_bytes, pae_bytes)
                return True
            except (InvalidSignature, ValueError, TypeError):
                continue
        return False
