"""AES-256-GCM envelope vault with per-workspace key isolation (F37 BYOK core).

Scheme (spec §4):

- KEK = 32-byte master key, **versioned** via the injected :class:`KeyProvider`
  (env-backed in prod: ``FORGE_VAULT_KEYS="1:<b64>,2:<b64>"`` +
  ``FORGE_VAULT_ACTIVE_KEY_VERSION``; fixed-key in tests).
- Per-workspace DEK = ``HKDF-SHA256(ikm=KEK_v, salt=workspace_id.bytes,
  info=b"forge-vault-dek", length=32)`` — leaking one workspace's derived key
  never exposes another.
- Blob = ``b"\\x01"`` (format ver) ``+ key_version (1B) + nonce (12B) +
  AESGCM(dek).encrypt(nonce, plaintext, aad=workspace_id.bytes)``. The
  workspace id in the **AAD** binds the ciphertext to its tenant: a blob copied
  to another workspace fails authentication on decrypt.
- ``rotate`` decrypts under the embedded version and re-encrypts under the
  active version (KEK rotation with zero plaintext exposure outside process).
"""

from __future__ import annotations

import base64
import binascii
import os
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from forge_auth.errors import DecryptionError, KeyMaterialError, KeyRotationError
from forge_contracts.auth import KeyProvider

__all__ = [
    "FORMAT_VERSION",
    "NONCE_SIZE",
    "EnvKeyProvider",
    "SecretVault",
    "StaticKeyProvider",
    "parse_vault_keys",
]

#: First byte of every blob — lets the wire format evolve.
FORMAT_VERSION = 1
#: AES-GCM standard 96-bit nonce.
NONCE_SIZE = 12
_KEY_SIZE = 32
_HKDF_INFO = b"forge-vault-dek"
_HEADER_SIZE = 2 + NONCE_SIZE  # format byte + key-version byte + nonce

#: Env var carrying the versioned KEK map, e.g. ``"1:<b64-32B>,2:<b64-32B>"``.
VAULT_KEYS_ENV = "FORGE_VAULT_KEYS"
#: Env var selecting the active KEK version for new encryptions.
ACTIVE_VERSION_ENV = "FORGE_VAULT_ACTIVE_KEY_VERSION"


def _check_key(version: int, key: bytes) -> bytes:
    if not 1 <= version <= 255:
        raise KeyMaterialError(f"KEK version must be 1..255, got {version}")
    if len(key) != _KEY_SIZE:
        raise KeyMaterialError(f"KEK v{version} must be exactly {_KEY_SIZE} bytes, got {len(key)}")
    return bytes(key)


class StaticKeyProvider:
    """Fixed KEK map (deterministic tests / embedders). Satisfies ``KeyProvider``."""

    def __init__(self, keys: dict[int, bytes], *, active: int | None = None) -> None:
        if not keys:
            raise KeyMaterialError("at least one KEK version is required")
        self._keys = {v: _check_key(v, k) for v, k in keys.items()}
        self._active = active if active is not None else max(self._keys)
        if self._active not in self._keys:
            raise KeyMaterialError(f"active KEK version {self._active} is not in the key map")

    def get(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError:
            raise KeyMaterialError(f"unknown KEK version {version}") from None

    def active_version(self) -> int:
        return self._active


def parse_vault_keys(raw: str) -> dict[int, bytes]:
    """Parse the ``FORGE_VAULT_KEYS`` format (``"1:<b64>,2:<b64>"``)."""
    keys: dict[int, bytes] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        version_str, _, b64 = entry.partition(":")
        try:
            version = int(version_str)
        except ValueError:
            raise KeyMaterialError(
                f"malformed {VAULT_KEYS_ENV} entry {entry!r}: version must be an integer"
            ) from None
        try:
            key = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise KeyMaterialError(
                f"malformed {VAULT_KEYS_ENV} entry for version {version}: invalid base64"
            ) from None
        keys[version] = _check_key(version, key)
    if not keys:
        raise KeyMaterialError(f"{VAULT_KEYS_ENV} contained no usable KEK entries")
    return keys


class EnvKeyProvider(StaticKeyProvider):
    """KEK provider backed by ``FORGE_VAULT_KEYS`` / ``FORGE_VAULT_ACTIVE_KEY_VERSION``.

    Fails closed: a missing or malformed env var raises
    :class:`KeyMaterialError` at construction (never a silent insecure default).
    """

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = environ if environ is not None else dict(os.environ)
        raw = env.get(VAULT_KEYS_ENV, "").strip()
        if not raw:
            raise KeyMaterialError(
                f"{VAULT_KEYS_ENV} must be set (versioned KEK map '1:<base64-32B>'); "
                'generate a key with: python -c "import os,base64;'
                'print(base64.b64encode(os.urandom(32)).decode())"'
            )
        keys = parse_vault_keys(raw)
        active_raw = env.get(ACTIVE_VERSION_ENV, "").strip()
        active: int | None = None
        if active_raw:
            try:
                active = int(active_raw)
            except ValueError:
                raise KeyMaterialError(
                    f"{ACTIVE_VERSION_ENV} must be an integer, got {active_raw!r}"
                ) from None
        super().__init__(keys, active=active)


class SecretVault:
    """Envelope encryption over an injected KEK provider. Satisfies ``Vault``."""

    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    def _dek(self, version: int, workspace_id: UUID) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=_KEY_SIZE,
            salt=workspace_id.bytes,
            info=_HKDF_INFO,
        ).derive(self._keys.get(version))

    def encrypt(self, plaintext: str, *, workspace_id: UUID) -> bytes:
        version = self._keys.active_version()
        nonce = os.urandom(NONCE_SIZE)
        ct = AESGCM(self._dek(version, workspace_id)).encrypt(
            nonce, plaintext.encode("utf-8"), workspace_id.bytes
        )
        return bytes([FORMAT_VERSION, version]) + nonce + ct

    def _decrypt_parts(self, blob: bytes, workspace_id: UUID) -> tuple[int, str]:
        """Return ``(embedded_key_version, plaintext)`` or raise DecryptionError."""
        if len(blob) < _HEADER_SIZE + 16 or blob[0] != FORMAT_VERSION:
            raise DecryptionError("malformed or unsupported vault blob")
        version = blob[1]
        nonce = blob[2:_HEADER_SIZE]
        ct = blob[_HEADER_SIZE:]
        try:
            dek = self._dek(version, workspace_id)
        except KeyMaterialError as exc:
            raise DecryptionError("vault blob references an unknown KEK version") from exc
        try:
            pt = AESGCM(dek).decrypt(nonce, ct, workspace_id.bytes)
        except InvalidTag as exc:
            raise DecryptionError(
                "vault blob failed authentication (tampered, wrong key, or wrong workspace)"
            ) from exc
        return version, pt.decode("utf-8")

    def decrypt(self, blob: bytes, *, workspace_id: UUID) -> str:
        """Decrypt a blob for ``workspace_id`` (raises on any mismatch/tamper)."""
        return self._decrypt_parts(blob, workspace_id)[1]

    def rotate(self, blob: bytes, *, workspace_id: UUID) -> bytes:
        """Re-encrypt ``blob`` under the active KEK version (crypto rotation)."""
        try:
            _, plaintext = self._decrypt_parts(blob, workspace_id)
        except DecryptionError as exc:
            raise KeyRotationError(f"cannot rotate vault blob: {exc}") from exc
        return self.encrypt(plaintext, workspace_id=workspace_id)
