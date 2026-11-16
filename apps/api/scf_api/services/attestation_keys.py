"""The key attestations are signed and verified with (#175).

``FORGE_ATTEST_SIGNING_KEY`` wins when it is set, and fails closed when it is
malformed. Otherwise every process uses the deployment's stored key: the first
process that needs one generates it and inserts it into ``attestation_signing_key``
(one row; a process that loses the insert race reads the winner's), encrypted
under a subkey of the instance master key ``FORGE_SECRET_KEY``. So the API, the
worker and a restarted process all sign and verify with one key.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scf_api.auth.crypto import InvalidTokenError, SecretCipher, default_cipher
from scf_api.auth.service import _resolve_master_key, _subkey
from scf_auth.errors import KeyMaterialError
from scf_db.models.attestation_key import SIGNING_KEY_ROW, AttestationSigningKey
from scf_obs.attest.signing import (
    SIGNING_KEY_ENV,
    EnvSigningKeyProvider,
    KeyPair,
    SigningKey,
    dev_ephemeral_key_allowed,
)

__all__ = ["signing_key"]

#: Key separation: the stored key's cipher is a subkey of the master key.
_SUBKEY_LABEL = b"scf-attest-signing-key"


def signing_key(session: Session, *, environ: Mapping[str, str] | None = None) -> SigningKey:
    """The deployment's attestation key, read over its own session on ``session``'s bind.

    The stored key is read and, when absent, inserted in a separate transaction,
    so the caller's pending work is neither committed nor rolled back.
    """
    env = dict(environ if environ is not None else os.environ)
    if env.get(SIGNING_KEY_ENV, "").strip():
        return EnvSigningKeyProvider(env)  # set: it wins; malformed raises
    cipher = default_cipher(_subkey(_resolve_master_key(None), _SUBKEY_LABEL))
    with Session(bind=session.get_bind()) as own:
        row = own.get(AttestationSigningKey, SIGNING_KEY_ROW)
        if row is None:
            row = _store_new_key(own, cipher)
        try:
            return KeyPair.from_seed_b64(
                cipher.decrypt(row.private_key_encrypted), source="the stored attestation key"
            )
        except InvalidTokenError:
            if dev_ephemeral_key_allowed(env):  # its master key is per process, too
                return KeyPair.ephemeral()
            raise KeyMaterialError(
                "the stored attestation signing key cannot be decrypted with this "
                "FORGE_SECRET_KEY: set the master key it was stored under, or set "
                f"{SIGNING_KEY_ENV}"
            ) from None


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
