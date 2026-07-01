"""Integration marketplace SDK (slice F32).

The pure, security-critical core beneath the ``apps/api`` marketplace router and
the ``apps/worker`` catalog-sync tasks: canonical hashing + Ed25519 verification
(the trust boundary), fail-closed schema validation via the authoritative F09/F11
loaders, and the least-privilege install security floor. No FastAPI, no DB, no
implicit network — and no package ever executes code (declarative YAML only).
"""

from __future__ import annotations

from forge_marketplace.catalog import (
    compare_semver,
    has_newer_compatible,
    is_compatible,
    latest_compatible,
    parse_semver,
)
from forge_marketplace.errors import (
    ForgeVersionIncompatible,
    HashMismatch,
    MarketplaceError,
    NameCollision,
    RegistryFetchError,
    SchemaInvalid,
    SignatureInvalid,
    SignatureRequired,
    SsrfBlocked,
    UnknownArtifactKind,
    UntrustedRegistry,
    YankedVersion,
)
from forge_marketplace.index import parse_index
from forge_marketplace.installer import (
    INSTALLERS,
    McpConnectorInstaller,
    SkillProfileInstaller,
    ValidatedArtifact,
    build_install_plan,
    get_installer,
)
from forge_marketplace.manifest import (
    canonical_artifact_bytes,
    compute_content_hash,
    compute_manifest_hash,
    dump_manifest,
    load_manifest,
)
from forge_marketplace.models import (
    ArtifactKind,
    InstallPlan,
    InstallRequest,
    InstallResult,
    InstallStatus,
    PackageAuthor,
    PackageManifest,
    RegistryIndex,
    RegistryIndexEntry,
    RegistryIndexVersion,
    RegistryType,
    SyncReport,
    TrustLevel,
    VerificationResult,
    VerificationStatus,
)
from forge_marketplace.packaging import build_package
from forge_marketplace.protocols import ArtifactInstaller, RegistryClient, SignatureVerifier
from forge_marketplace.registry_client import (
    GitRegistryClient,
    HttpIndexRegistryClient,
    guard_url,
)
from forge_marketplace.verifier import Ed25519SignatureVerifier, verify_version

__version__ = "0.1.0"

__all__ = [
    "INSTALLERS",
    "ArtifactInstaller",
    "ArtifactKind",
    "Ed25519SignatureVerifier",
    "ForgeVersionIncompatible",
    "GitRegistryClient",
    "HashMismatch",
    "HttpIndexRegistryClient",
    "InstallPlan",
    "InstallRequest",
    "InstallResult",
    "InstallStatus",
    "MarketplaceError",
    "McpConnectorInstaller",
    "NameCollision",
    "PackageAuthor",
    "PackageManifest",
    "RegistryClient",
    "RegistryFetchError",
    "RegistryIndex",
    "RegistryIndexEntry",
    "RegistryIndexVersion",
    "RegistryType",
    "SchemaInvalid",
    "SignatureInvalid",
    "SignatureRequired",
    "SignatureVerifier",
    "SkillProfileInstaller",
    "SsrfBlocked",
    "SyncReport",
    "TrustLevel",
    "UnknownArtifactKind",
    "UntrustedRegistry",
    "ValidatedArtifact",
    "VerificationResult",
    "VerificationStatus",
    "YankedVersion",
    "__version__",
    "build_install_plan",
    "build_package",
    "canonical_artifact_bytes",
    "compare_semver",
    "compute_content_hash",
    "compute_manifest_hash",
    "dump_manifest",
    "get_installer",
    "guard_url",
    "has_newer_compatible",
    "is_compatible",
    "latest_compatible",
    "load_manifest",
    "parse_index",
    "parse_semver",
    "verify_version",
]
