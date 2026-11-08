"""Author-side signing: the counterpart of :mod:`forge_marketplace.verifier`.

A registry version's ``signature`` is a detached Ed25519 signature over the
UTF-8 bytes of its ``manifest_hash``, base64-encoded. In-app publish never
signs (it stores ``signature=None``); signing happens offline with the
registry maintainer's key via ``forge-cli marketplace sign``.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key


class SigningKeyError(ValueError):
    """The key file is unusable. Messages never carry key material."""


def sign_manifest_hash(manifest_hash: str, private_key: Ed25519PrivateKey) -> str:
    """Base64 detached Ed25519 signature over ``manifest_hash.encode('utf-8')``."""
    return base64.b64encode(private_key.sign(manifest_hash.encode("utf-8"))).decode("ascii")


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PEM file or a raw 32-byte base64 file.

    Refuses a file with any group/other permission bits (``mode & 0o077``).
    """
    with open(path, "rb") as fh:
        # fstat on the open fd: the mode checked is the file actually read.
        if os.fstat(fh.fileno()).st_mode & 0o077:
            raise SigningKeyError(
                f"key file {path} is accessible by group/others; run: chmod 600 {path}"
            )
        data = fh.read()

    if data.lstrip().startswith(b"-----BEGIN"):
        try:
            key = load_pem_private_key(data, password=None)
        except (ValueError, TypeError):
            raise SigningKeyError(
                f"key file {path} is not an unencrypted PEM private key"
            ) from None
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningKeyError(f"key file {path} is not an Ed25519 private key")
        return key

    try:
        raw = base64.b64decode(data.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SigningKeyError(f"key file {path} is neither PEM nor base64") from None
    if len(raw) != 32:
        raise SigningKeyError(f"key file {path} must hold a raw 32-byte Ed25519 key")
    return Ed25519PrivateKey.from_private_bytes(raw)


__all__ = ["SigningKeyError", "load_private_key", "sign_manifest_hash"]
