"""Structural typing seams for the marketplace SDK (F32 §4).

Keeping these as :class:`typing.Protocol`s lets the API service inject real,
DB-backed / network implementations while the SDK core and its tests use pure
fakes — no FastAPI, DB, or network in the SDK.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from forge_marketplace.models import (
    ArtifactKind,
    PackageManifest,
    RegistryIndex,
)


class RegistryClient(Protocol):
    """Fetches a registry's index + individual package manifests."""

    async def fetch_index(self) -> RegistryIndex: ...

    async def fetch_manifest(self, manifest_uri: str) -> tuple[PackageManifest, bytes]:
        """Return the parsed manifest and the raw canonical manifest bytes."""
        ...


class SignatureVerifier(Protocol):
    """Detached Ed25519 verify. Pure; returns bool (never raises)."""

    def verify(self, *, payload: bytes, signature_b64: str, public_key_b64: str) -> bool: ...


class ArtifactInstaller(Protocol):
    """Validates an embedded artifact and materializes the local F09/F11 object."""

    kind: ArtifactKind

    def validate(self, artifact: dict) -> dict:
        """Validate via the authoritative loader, apply the security floor, return
        the normalized config dict. Raises ``SchemaInvalid`` on any violation."""
        ...

    async def apply(
        self,
        *,
        workspace_id: UUID,
        manifest: PackageManifest,
        actor: str,
        override_name: str | None,
    ) -> tuple[str, UUID]:
        """Create the local object via the F09/F11 service; return
        ``(target_kind, target_object_id)``. Never enables write / connects /
        stores secrets."""
        ...


__all__ = ["ArtifactInstaller", "RegistryClient", "SignatureVerifier"]
