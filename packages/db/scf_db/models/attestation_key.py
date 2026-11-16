"""The deployment's attestation signing key (#175).

One row, written once: the first process that needs a key and finds none stores
one, and every process after it (the API, the worker, a restart) signs and
verifies with that key. The private key is stored encrypted under a subkey of
the instance master key (``FORGE_SECRET_KEY``), never in the clear.
"""

from __future__ import annotations

from sqlalchemy import Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from scf_db.base import Base, TimestampMixin

#: The one row's primary key: a second insert conflicts instead of adding a key.
SIGNING_KEY_ROW = 1


class AttestationSigningKey(TimestampMixin, Base):
    """The Ed25519 key attestations are signed with when no key is configured."""

    __tablename__ = "attestation_signing_key"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    #: ``sha256`` of the raw public key, as the signer reports it.
    keyid: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The raw public key, base64.
    public_key_b64: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The 32-byte private seed, base64, encrypted with the master key's subkey.
    private_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


__all__ = ["SIGNING_KEY_ROW", "AttestationSigningKey"]
