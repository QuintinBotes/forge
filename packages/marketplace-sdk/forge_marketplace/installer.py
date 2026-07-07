"""Per-kind artifact validation + the least-privilege install security floor.

This module is the reason the marketplace can install *untrusted* third-party
content as safely as the platform's own defaults. It is **declarative-only**:
there is deliberately no ``eval`` / ``exec`` / ``__import__`` / dynamic-import
path here (AC20) — an installer only ever produces a *validated, normalized
config dict* that the API service turns into an F09 ``mcp_connection`` /
F11 ``skill_profile`` row. It never imports or runs anything a package ships.

Validation delegates to the **authoritative** loaders:

* ``mcp_connector`` -> ``forge_contracts.MCPConnection`` (the F09 schema), then the
  security floor: ``allow_write`` forced ``False``, ``stdio`` transport rejected,
  empty ``allowed_namespaces`` warned (over-broad), admin follow-ups attached.
* ``skill_profile`` -> ``forge_skill.load_profile`` (the F11 loader). A name that
  shadows a builtin becomes an audited *override* (warning, not a block).

Reserved kinds (``workflow_template`` / ``policy_template``) have **no** installer
registered until F21 / F04 land (F32 §12) -> :class:`UnknownArtifactKind`.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from forge_contracts import MCPConnection, SkillProfile
from forge_contracts.enums import MCPTransport
from forge_marketplace.catalog import is_compatible
from forge_marketplace.errors import SchemaInvalid, UnknownArtifactKind
from forge_marketplace.models import (
    ArtifactKind,
    InstallPlan,
    PackageManifest,
    VerificationResult,
)
from forge_skill import BUILTIN_PROFILE_NAMES
from forge_skill.loader import load_profile

# Advisory follow-ups an admin must perform *after* an MCP connector installs
# (it lands read-only + not-connected and never auto-OAuths — F32 §2 journey E).
MCP_FOLLOWUPS = [
    "review the endpoint / scope before connecting",
    "connect and supply credentials in MCP settings to activate",
]


class ValidatedArtifact(BaseModel):
    """The result of validating + applying the security floor to an artifact."""

    target_kind: str  # "mcp_connection" | "skill_profile"
    resolved_config: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    requires_admin_followup: list[str] = Field(default_factory=list)
    overrides_builtin: bool = False


class McpConnectorInstaller:
    """Validates an ``mcp_connector`` artifact + applies the F09 security floor."""

    kind = ArtifactKind.mcp_connector
    target_kind = "mcp_connection"

    def validate(self, artifact: dict) -> ValidatedArtifact:
        data = dict(artifact)
        # ``MCPConnection`` requires an ``id``; a template need not carry one, so
        # synthesize from the declared name/slug purely for schema validation.
        data.setdefault("id", str(data.get("name") or "pending"))
        try:
            conn = MCPConnection.model_validate(data)
        except ValidationError as exc:
            raise SchemaInvalid(
                f"mcp_connector artifact is not a valid MCPConnection: {exc}"
            ) from exc

        # Security floor (F32 §3.2 / AC8):
        if conn.transport is MCPTransport.STDIO:
            raise SchemaInvalid(
                "stdio transport is not permitted for marketplace MCP connectors "
                "(matches the F09 V1 limits)"
            )

        warnings: list[str] = []
        if bool(artifact.get("allow_write")):
            warnings.append(
                "declared allow_write=true was overridden to false "
                "(marketplace connectors install read-only)"
            )
        if not conn.allowed_namespaces:
            warnings.append(
                "allowed_namespaces is empty — the connector would be over-broad; "
                "scope it before connecting"
            )

        resolved = conn.model_dump(mode="json")
        resolved["allow_write"] = False  # never install a writable connector
        return ValidatedArtifact(
            target_kind=self.target_kind,
            resolved_config=resolved,
            warnings=warnings,
            requires_admin_followup=list(MCP_FOLLOWUPS),
            overrides_builtin=False,
        )


class SkillProfileInstaller:
    """Validates a ``skill_profile`` artifact via the F11 loader (fail-closed)."""

    kind = ArtifactKind.skill_profile
    target_kind = "skill_profile"

    def validate(self, artifact: dict) -> ValidatedArtifact:
        try:
            profile: SkillProfile = load_profile(dict(artifact))
        except (ValidationError, ValueError, TypeError) as exc:
            raise SchemaInvalid(f"skill_profile artifact failed F11 validation: {exc}") from exc

        warnings: list[str] = []
        overrides = profile.name in BUILTIN_PROFILE_NAMES
        if overrides:
            warnings.append(
                f"name '{profile.name}' shadows a builtin profile — "
                "installing creates a workspace override"
            )
        return ValidatedArtifact(
            target_kind=self.target_kind,
            resolved_config=profile.model_dump(mode="json"),
            warnings=warnings,
            requires_admin_followup=[],
            overrides_builtin=overrides,
        )


#: The registered installers. Reserved kinds are intentionally absent (F32 §12).
INSTALLERS: dict[ArtifactKind, McpConnectorInstaller | SkillProfileInstaller] = {
    ArtifactKind.mcp_connector: McpConnectorInstaller(),
    ArtifactKind.skill_profile: SkillProfileInstaller(),
}


def get_installer(kind: ArtifactKind) -> McpConnectorInstaller | SkillProfileInstaller:
    """Return the installer for ``kind`` or raise :class:`UnknownArtifactKind`."""
    installer = INSTALLERS.get(kind)
    if installer is None:
        raise UnknownArtifactKind(
            f"no installer registered for kind '{kind.value}' (reserved — requires F21/F04)"
        )
    return installer


def build_install_plan(
    *,
    manifest: PackageManifest,
    version: str,
    verification: VerificationResult,
    forge_version: str,
    registry_id: UUID | None = None,
    override_name: str | None = None,
) -> InstallPlan:
    """Assemble a side-effect-free :class:`InstallPlan` (fetch/verify already done).

    Never writes anything. Encodes every install-blocking condition into
    ``blocked`` / ``block_reason`` so ``/preview`` can render the exact gate the
    admin faces and ``/install`` can refuse without partial work.
    """
    base = {
        "registry_id": registry_id,
        "kind": manifest.kind,
        "slug": manifest.slug,
        "version": version,
        "verification": verification,
    }

    if verification.blocked:
        return InstallPlan(
            **base,  # type: ignore[arg-type]
            resolved_config={},
            blocked=True,
            block_reason=verification.detail or f"verification failed: {verification.status.value}",
        )

    if not is_compatible(manifest.min_forge_version, forge_version):
        return InstallPlan(
            **base,  # type: ignore[arg-type]
            resolved_config={},
            blocked=True,
            block_reason=(
                f"requires Forge >= {manifest.min_forge_version} (running {forge_version})"
            ),
        )

    installer = get_installer(manifest.kind)
    try:
        validated = installer.validate(manifest.artifact)
    except SchemaInvalid as exc:
        return InstallPlan(
            **base,  # type: ignore[arg-type]
            resolved_config={},
            blocked=True,
            block_reason=str(exc),
        )

    resolved = dict(validated.resolved_config)
    if override_name:
        # The name the local object will take (skill name / mcp connection name).
        resolved["name"] = override_name

    return InstallPlan(
        **base,  # type: ignore[arg-type]
        resolved_config=resolved,
        warnings=validated.warnings,
        requires_admin_followup=validated.requires_admin_followup,
        overrides_builtin=validated.overrides_builtin,
        blocked=False,
    )


__all__ = [
    "INSTALLERS",
    "MCP_FOLLOWUPS",
    "McpConnectorInstaller",
    "SkillProfileInstaller",
    "ValidatedArtifact",
    "build_install_plan",
    "get_installer",
]
