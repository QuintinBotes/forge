"""Integration marketplace SDK (slice F32).

The pure, security-critical core beneath the ``apps/api`` marketplace router and
the ``apps/worker`` catalog-sync tasks: canonical hashing + Ed25519 verification
(the trust boundary), fail-closed schema validation via the authoritative F09/F11
loaders, and the least-privilege install security floor. No FastAPI, no DB, no
implicit network — and no package ever executes code (declarative YAML only).
"""

from __future__ import annotations

from scf_marketplace.catalog import (
    compare_semver,
    has_newer_compatible,
    is_compatible,
    latest_compatible,
    parse_semver,
)
from scf_marketplace.errors import (
    HashMismatch,
    MarketplaceError,
    NameCollision,
    RegistryFetchError,
    ScfVersionIncompatible,
    SchemaInvalid,
    SignatureInvalid,
    SignatureRequired,
    SsrfBlocked,
    UnknownArtifactKind,
    UntrustedRegistry,
    YankedVersion,
)
from scf_marketplace.index import parse_index
from scf_marketplace.installer import (
    INSTALLERS,
    McpConnectorInstaller,
    SkillProfileInstaller,
    ValidatedArtifact,
    WorkflowTemplateArtifact,
    WorkflowTemplateInstaller,
    build_install_plan,
    default_secret_like,
    get_installer,
)
from scf_marketplace.manifest import (
    canonical_artifact_bytes,
    compute_content_hash,
    compute_manifest_hash,
    dump_manifest,
    load_manifest,
)
from scf_marketplace.models import (
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
from scf_marketplace.packaging import build_package
from scf_marketplace.policy import kind_policy_violation
from scf_marketplace.protocols import ArtifactInstaller, RegistryClient, SignatureVerifier
from scf_marketplace.registry_client import (
    GitRegistryClient,
    HttpIndexRegistryClient,
    guard_url,
)
from scf_marketplace.signing import SigningKeyError, load_private_key, sign_manifest_hash
from scf_marketplace.verifier import Ed25519SignatureVerifier, verify_package, verify_version

__version__ = "0.1.0"

__all__ = [
    "INSTALLERS",
    "ArtifactInstaller",
    "ArtifactKind",
    "Ed25519SignatureVerifier",
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
    "ScfVersionIncompatible",
    "SchemaInvalid",
    "SignatureInvalid",
    "SignatureRequired",
    "SignatureVerifier",
    "SigningKeyError",
    "SkillProfileInstaller",
    "SsrfBlocked",
    "SyncReport",
    "TrustLevel",
    "UnknownArtifactKind",
    "UntrustedRegistry",
    "ValidatedArtifact",
    "VerificationResult",
    "VerificationStatus",
    "WorkflowTemplateArtifact",
    "WorkflowTemplateInstaller",
    "YankedVersion",
    "__version__",
    "build_install_plan",
    "build_package",
    "canonical_artifact_bytes",
    "compare_semver",
    "compute_content_hash",
    "compute_manifest_hash",
    "default_secret_like",
    "dump_manifest",
    "get_installer",
    "guard_url",
    "has_newer_compatible",
    "is_compatible",
    "kind_policy_violation",
    "latest_compatible",
    "load_manifest",
    "load_private_key",
    "parse_index",
    "parse_semver",
    "sign_manifest_hash",
    "verify_package",
    "verify_version",
]
