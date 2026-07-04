"""Versioned KEK material for envelope encryption (HARD-13).

Envelope encryption wraps a per-secret data key (DEK) under a key-encryption key
(KEK). Rotating the KEK must not require touching BYOK plaintext, so KEKs are
*versioned*: every stored blob records which KEK version wrapped its DEK, and the
:class:`KeyRing` resolves the material for any version still configured.

The current KEK is ``FORGE_SECRET_KEY``; previous versions kept alive during a
rotation window are ``FORGE_SECRET_KEY_V<n>``. All material is resolved through
the :mod:`~forge_api.auth.providers` secret provider (env / file / Vault), never a
direct ``os.environ`` read, so a Vault- or file-backed KEK works unchanged.
"""

from __future__ import annotations

from forge_api.auth.providers import SecretProvider, get_default_provider

#: KEK version is serialised in a single byte of the envelope header.
_MAX_KEK_VERSION = 255
_MIN_KEY_SIZE = 16


class KeyRing:
    """An immutable set of versioned KEKs with a designated *current* version."""

    def __init__(self, keys: dict[int, bytes], current_version: int) -> None:
        if not keys:
            raise ValueError("KeyRing requires at least one KEK version")
        if current_version not in keys:
            raise ValueError(
                f"current_version {current_version} has no KEK material "
                f"(have versions {sorted(keys)})"
            )
        for version, material in keys.items():
            if not (1 <= version <= _MAX_KEK_VERSION):
                raise ValueError(f"KEK version {version} out of range 1..{_MAX_KEK_VERSION}")
            if len(material) < _MIN_KEY_SIZE:
                raise ValueError(
                    f"KEK v{version} must be at least {_MIN_KEY_SIZE} bytes, got {len(material)}"
                )
        self._keys = dict(keys)
        self.current_version = current_version

    def kek(self, version: int) -> bytes:
        """Return the KEK material for ``version`` or raise ``KeyError``."""
        try:
            return self._keys[version]
        except KeyError as exc:
            raise KeyError(
                f"no KEK configured for version {version}; set FORGE_SECRET_KEY_V{version} "
                f"(have versions {sorted(self._keys)})"
            ) from exc

    def current_kek(self) -> bytes:
        """Return the KEK material for the current version."""
        return self._keys[self.current_version]

    def versions(self) -> list[int]:
        """Return all configured KEK versions, ascending."""
        return sorted(self._keys)

    @classmethod
    def from_provider(
        cls,
        provider: SecretProvider | None = None,
        *,
        current_version: int | None = None,
        require: bool = True,
    ) -> KeyRing | None:
        """Build a :class:`KeyRing` from ``FORGE_SECRET_KEY`` (+ ``_V<n>`` history).

        ``FORGE_SECRET_KEY`` is the current KEK; ``FORGE_SECRET_KEY_V<n>`` are
        older versions retained during a rotation window. ``current_version``
        defaults to ``FORGE_SECRET_KEY_VERSION`` when set, else the highest
        configured version, else 1. When no current key is resolvable and
        ``require`` is true (production), this raises; with ``require=False`` it
        returns ``None`` so a dev caller can fall back to an ephemeral key.
        """
        resolver = provider or get_default_provider()

        keys: dict[int, bytes] = {}
        for version in range(1, _MAX_KEK_VERSION + 1):
            material = resolver.get(f"FORGE_SECRET_KEY_V{version}")
            if material:
                keys[version] = material.encode("utf-8")

        if current_version is None:
            declared = resolver.get("FORGE_SECRET_KEY_VERSION")
            if declared:
                current_version = int(declared)
        if current_version is None:
            current_version = max(keys) if keys else 1

        current = resolver.get("FORGE_SECRET_KEY")
        if current:
            # The current key always occupies the current-version slot, even if a
            # stale FORGE_SECRET_KEY_V<current> was left set.
            keys[current_version] = current.encode("utf-8")

        if not keys:
            if require:
                raise RuntimeError(
                    "FORGE_SECRET_KEY must be set to build a KeyRing (no current KEK "
                    "and no versioned KEKs resolved). Generate one with "
                    "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`."
                )
            return None

        return cls(keys, current_version)


__all__ = ["KeyRing"]
