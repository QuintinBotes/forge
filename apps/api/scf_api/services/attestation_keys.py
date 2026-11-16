"""The key attestations are signed and verified with (#175).

``FORGE_ATTEST_SIGNING_KEY`` wins when it is set, and fails closed when it is
malformed. Otherwise every process uses the deployment's stored key: the first
process that needs one generates it and inserts it into ``attestation_signing_key``
(one row; a process that loses the insert race reads the winner's), encrypted
under a subkey of the instance master key ``FORGE_SECRET_KEY``. So the API, the
worker and a restarted process all sign and verify with one key.

Signing needs the master key; verifying does not: :func:`verification_key` reads
only the public key, which is stored in the clear.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scf_api.auth.crypto import InvalidTokenError, SecretCipher, default_cipher
from scf_api.auth.providers import resolve_secret
from scf_api.auth.service import _keyring_for_master, _resolve_master_key, _subkey
from scf_auth.errors import KeyMaterialError
from scf_db.models.attestation_key import SIGNING_KEY_ROW, AttestationSigningKey
from scf_obs.attest.signing import (
    SIGNING_KEY_ENV,
    EnvSigningKeyProvider,
    KeyPair,
    SigningKey,
    dev_ephemeral_key_allowed,
)

__all__ = ["signing_key", "verification_key"]

#: Key separation: the stored key's cipher is a subkey of the master key.
_SUBKEY_LABEL = b"scf-attest-signing-key"
#: The development key under FORGE_DEV_INSECURE with no master key: one per process.
_dev_key: KeyPair | None = None


def signing_key(session: Session, *, environ: Mapping[str, str] | None = None) -> SigningKey:
    """The deployment's attestation key, read over its own session on ``session``'s bind.

    The stored key is read and, when absent, inserted in a separate transaction,
    so the caller's pending work is neither committed nor rolled back. A key stored
    under an older master key (``FORGE_SECRET_KEY_V<n>``, kept during a rotation)
    is re-encrypted under the current one.
    """
    env = dict(environ if environ is not None else os.environ)
    if env.get(SIGNING_KEY_ENV, "").strip():
        return EnvSigningKeyProvider(env)  # set: it wins; malformed raises
    if not _master_key_configured() and dev_ephemeral_key_allowed(env):
        return _process_dev_key()  # the dev master key is random per call: store nothing
    master = _resolve_master_key(None)  # unset and no opt-in: refuses
    with Session(bind=session.get_bind()) as own:
        row = own.get(AttestationSigningKey, SIGNING_KEY_ROW)
        if row is None:
            row = _store_new_key(own, _cipher(master))
        return _decrypt(own, row, master)


def verification_key(session: Session, *, environ: Mapping[str, str] | None = None) -> str | None:
    """The base64 public key attestations verify against, or ``None`` before any is stored.

    Reads only the public half, through ``session``: no master key, no second
    connection, nothing written.
    """
    env = dict(environ if environ is not None else os.environ)
    if env.get(SIGNING_KEY_ENV, "").strip():
        return EnvSigningKeyProvider(env).public_key_b64  # malformed raises
    if not _master_key_configured() and dev_ephemeral_key_allowed(env):
        return _process_dev_key().public_key_b64
    row = session.get(AttestationSigningKey, SIGNING_KEY_ROW)
    return row.public_key_b64 if row is not None else None


def _master_key_configured() -> bool:
    return bool(resolve_secret("FORGE_SECRET_KEY") or resolve_secret("SECRET_KEY"))


def _process_dev_key() -> KeyPair:
    global _dev_key
    if _dev_key is None:
        _dev_key = KeyPair.ephemeral()
    return _dev_key


def _cipher(master: bytes) -> SecretCipher:
    return default_cipher(_subkey(master, _SUBKEY_LABEL))


def _decrypt(session: Session, row: AttestationSigningKey, master: bytes) -> KeyPair:
    """The stored key, under the current master key or an older version of it."""
    current = _cipher(master)
    ring = _keyring_for_master(master)
    older = [ring.kek(v) for v in reversed(ring.versions()) if ring.kek(v) != master]
    for candidate in [master, *older]:
        try:
            seed_b64 = _cipher(candidate).decrypt(row.private_key_encrypted)
        except InvalidTokenError:
            continue
        if candidate != master:  # rotated: keep it readable after the old key is retired
            row.private_key_encrypted = current.encrypt(seed_b64)
            session.commit()
        return KeyPair.from_seed_b64(seed_b64, source="the stored attestation key")
    raise KeyMaterialError(
        "the stored attestation signing key cannot be decrypted with this FORGE_SECRET_KEY "
        "or its FORGE_SECRET_KEY_V<n> versions: set the master key it was stored under, "
        f"or set {SIGNING_KEY_ENV}"
    )


def _store_new_key(session: Session, cipher: SecretCipher) -> AttestationSigningKey:
    """Insert a fresh key unless another process stored one first; return the stored one."""
    key = KeyPair(Ed25519PrivateKey.generate())
    session.add(
        AttestationSigningKey(
            id=SIGNING_KEY_ROW,
            keyid=key.keyid,
            public_key_b64=key.public_key_b64,
            private_key_encrypted=cipher.encrypt(key.seed_b64()),
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # another process stored the key: use theirs
    stored = session.get(AttestationSigningKey, SIGNING_KEY_ROW)
    assert stored is not None  # inserted here or by the process that won the race
    return stored
