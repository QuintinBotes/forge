"""Author-side packaging: ``forge marketplace package`` (F32 §2 journey I / AC20).

Turns a raw F09 ``mcp_connection`` / F11 ``SkillProfile`` artifact into a
canonical :class:`PackageManifest` with a computed ``content_hash`` that
re-verifies with :func:`~forge_marketplace.manifest.load_manifest`. Signing +
opening a PR to a registry git repo is the OSS publishing path (out of the SDK).
"""

from __future__ import annotations

from typing import Any

from forge_marketplace.installer import get_installer
from forge_marketplace.manifest import compute_content_hash, compute_manifest_hash
from forge_marketplace.models import ArtifactKind, PackageAuthor, PackageManifest


def build_package(
    *,
    kind: ArtifactKind | str,
    artifact: dict[str, Any],
    slug: str,
    name: str,
    version: str,
    summary: str,
    description: str | None = None,
    authors: list[PackageAuthor] | None = None,
    license: str = "Apache-2.0",
    homepage: str | None = None,
    repository: str | None = None,
    tags: list[str] | None = None,
    min_forge_version: str | None = None,
    validate_artifact: bool = True,
) -> PackageManifest:
    """Build a signed-ready :class:`PackageManifest` with a computed ``content_hash``.

    When ``validate_artifact`` (default) the embedded artifact is run through the
    same authoritative installer validation the consumer applies, so a package
    that would fail to install cannot be produced silently.
    """
    kind = ArtifactKind(kind)
    if validate_artifact:
        get_installer(kind).validate(artifact)  # raises SchemaInvalid on bad artifact

    return PackageManifest(
        kind=kind,
        slug=slug,
        name=name,
        version=version,
        summary=summary,
        description=description,
        authors=authors or [],
        license=license,
        homepage=homepage,
        repository=repository,
        tags=tags or [],
        min_forge_version=min_forge_version,
        artifact=artifact,
        content_hash=compute_content_hash(artifact),
    )


def manifest_hash(manifest: PackageManifest) -> str:
    """Convenience re-export: the ``sha256:<hex>`` a registry signs per version."""
    return compute_manifest_hash(manifest)


__all__ = ["build_package", "manifest_hash"]
