"""Pydantic v2 models + enums for the integration marketplace (F32 §4).

These are the *authoritative* wire/contract types shared by the SDK, the API
router, and the worker sync tasks:

* :class:`PackageManifest` — the ``forge-package.yaml`` an author publishes
  (``extra='forbid'`` so a typo cannot smuggle an unrecognised field).
* :class:`RegistryIndex` (+ entry/version) — the signed ``index.json`` a
  registry publishes; parsed fail-closed.
* :class:`VerificationResult`, :class:`InstallRequest`, :class:`InstallPlan`,
  :class:`InstallResult`, :class:`SyncReport` — the install trust-boundary DTOs.

No secrets ever appear here (the schema has no credential fields): MCP
credentials are supplied by the admin post-install through the F09/F37 vault.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([-+].+)?$")


class ArtifactKind(StrEnum):
    """The first-class distributable artifact kinds.

    ``workflow_template`` / ``policy_template`` are *reserved*: the enum values
    exist so a registry can advertise them, but their installers are only
    registered when F21 / F04 provide the loaders + targets (F32 §12).
    """

    mcp_connector = "mcp_connector"
    skill_profile = "skill_profile"
    workflow_template = "workflow_template"
    policy_template = "policy_template"


class RegistryType(StrEnum):
    git = "git"
    http_index = "http_index"


class TrustLevel(StrEnum):
    official = "official"
    trusted = "trusted"
    community = "community"
    unverified = "unverified"


class VerificationStatus(StrEnum):
    verified = "verified"  # content hash ok AND signature ok against a trusted key
    unsigned = "unsigned"  # content hash ok, no signature present
    untrusted_registry = "untrusted_registry"  # signature present but registry has no key
    signature_invalid = "signature_invalid"  # hard block
    hash_mismatch = "hash_mismatch"  # hard block


class InstallStatus(StrEnum):
    pending = "pending"
    installed = "installed"
    update_available = "update_available"
    failed = "failed"
    uninstalled = "uninstalled"


class PackageAuthor(BaseModel):
    name: str
    email: str | None = None
    url: str | None = None


class PackageManifest(BaseModel):
    """The ``forge-package.yaml`` schema. ``extra='forbid'`` => fail-closed on typos."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    kind: ArtifactKind
    slug: str
    name: str
    version: str  # semver
    summary: str
    description: str | None = None
    authors: list[PackageAuthor] = Field(default_factory=list)
    license: str = "Apache-2.0"
    homepage: str | None = None
    repository: str | None = None
    tags: list[str] = Field(default_factory=list)
    min_forge_version: str | None = None  # semver gate
    artifact: dict  # embedded body: F09 mcp_connection | F11 SkillProfile
    content_hash: str  # sha256:<hex> over canonical_artifact_bytes(artifact)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must be kebab-case ^[a-z][a-z0-9-]{1,63}$")
        return v

    @field_validator("version", "min_forge_version")
    @classmethod
    def _semver(cls, v: str | None) -> str | None:
        if v is not None and not SEMVER_RE.match(v):
            raise ValueError("must be semver MAJOR.MINOR.PATCH")
        return v

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, v: str) -> str:
        if not HASH_RE.match(v):
            raise ValueError("content_hash must be sha256:<64 hex>")
        return v


class RegistryIndexVersion(BaseModel):
    version: str
    content_hash: str  # sha256:<hex> of artifact
    manifest_hash: str  # sha256:<hex> of canonical manifest JSON (signed payload)
    signature: str | None = None  # base64 detached Ed25519 sig over manifest_hash bytes
    manifest_uri: str  # resolvable URI to forge-package.yaml
    min_forge_version: str | None = None
    published_at: datetime
    yanked: bool = False
    yanked_reason: str | None = None


class RegistryIndexEntry(BaseModel):
    kind: ArtifactKind
    slug: str
    name: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    homepage: str | None = None
    repository: str | None = None
    license: str = "Apache-2.0"
    latest_version: str
    versions: list[RegistryIndexVersion]


class RegistryIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    registry_name: str
    public_key: str | None = None  # Ed25519 verify key (base64); informational copy
    generated_at: datetime
    entries: list[RegistryIndexEntry]


class VerificationResult(BaseModel):
    status: VerificationStatus
    content_hash_ok: bool
    signature_ok: bool | None = None  # None when no signature/key involved
    detail: str | None = None

    @property
    def blocked(self) -> bool:  # hard-block statuses
        return self.status in (
            VerificationStatus.hash_mismatch,
            VerificationStatus.signature_invalid,
        )

    @property
    def needs_acknowledgement(self) -> bool:
        """Soft-gated statuses that require an explicit admin acknowledgement."""
        return self.status in (
            VerificationStatus.unsigned,
            VerificationStatus.untrusted_registry,
        )


class InstallRequest(BaseModel):
    registry_id: UUID
    kind: ArtifactKind
    slug: str
    version: str | None = None  # None => latest non-yanked compatible
    acknowledge_unverified: bool = False  # required for unsigned/untrusted_registry
    override_name: str | None = None  # optional rename to avoid collisions


class InstallPlan(BaseModel):
    registry_id: UUID | None = None
    kind: ArtifactKind
    slug: str
    version: str
    verification: VerificationResult
    resolved_config: dict  # MCPConnectionConfig.model_dump() | SkillProfile.model_dump()
    warnings: list[str] = Field(default_factory=list)
    requires_admin_followup: list[str] = Field(default_factory=list)
    overrides_builtin: bool = False
    blocked: bool = False
    block_reason: str | None = None


class InstallResult(BaseModel):
    installation_id: UUID
    target_kind: str  # "mcp_connection" | "skill_profile"
    target_object_id: UUID
    status: InstallStatus
    version: str
    verification: VerificationResult
    warnings: list[str] = Field(default_factory=list)


class SyncReport(BaseModel):
    registry_id: UUID
    listings_upserted: int = 0
    versions_upserted: int = 0
    listings_pruned: int = 0
    status: str = "ok"  # "ok" | "error"
    error: str | None = None


__all__ = [
    "HASH_RE",
    "SEMVER_RE",
    "SLUG_RE",
    "ArtifactKind",
    "InstallPlan",
    "InstallRequest",
    "InstallResult",
    "InstallStatus",
    "PackageAuthor",
    "PackageManifest",
    "RegistryIndex",
    "RegistryIndexEntry",
    "RegistryIndexVersion",
    "RegistryType",
    "SyncReport",
    "TrustLevel",
    "VerificationResult",
    "VerificationStatus",
]
