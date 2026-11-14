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
* ``workflow_template`` -> ``forge_contracts.parse_line`` + ``validate_line`` (a DSL v2
  line, spec K §4.4), after a secret scan; bundled names are reserved.

``policy_template`` is reserved: **no** installer is registered until F04 lands
(F32 §12) -> :class:`UnknownArtifactKind`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from forge_contracts import (
    AgentStation,
    HumanStation,
    LineDefinition,
    MCPConnection,
    ServiceStation,
    SkillProfile,
    parse_line,
    validate_line,
)
from forge_contracts.enums import MCPTransport
from forge_contracts.lines import SERVICE_CATALOG
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

    target_kind: str  # "mcp_connection" | "skill_profile" | "workflow_definition"
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


# --------------------------------------------------------------------------- #
# workflow_template (spec K §4.4)                                              #
# --------------------------------------------------------------------------- #

WORKFLOW_TEMPLATE_MAX_BYTES = 128 * 1024
#: The bundled definition names (``forge_workflow.editor.store.default_bundled_loader``);
#: the SDK cannot import ``forge_workflow``, so a test pins this copy.
RESERVED_LINE_NAMES = frozenset({"default_feature", "incident"})
LINE_FOLLOWUPS = ["requires the temporal engine", "bind services on the worker"]

# Kept as a pattern string (``re`` caches it): the no-dynamic-execution test bans
# any call spelled like the builtin compile in this module.
_SECRET_VALUE_PATTERN = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|gh[po]_[A-Za-z0-9]{20,}"  # no leading boundary: a token glued to a word still counts
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}(?![0-9A-Z])"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    # sk- and eyJ occur inside words ("task-…", "theyJ…"): only a letter or digit hides them.
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"
    r"|(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"  # a JWT
)
_SECRET_KEY_HINTS = ("password", "secret", "token", "api_key", "private_key", "credential")


def default_secret_like(text: str) -> bool:
    """The one detector for API and CLI: private-key blocks and common token shapes.

    Shapes only, never words like "basic" or "bearer", which ordinary objectives use.
    """
    return re.search(_SECRET_VALUE_PATTERN, text) is not None


class WorkflowTemplateArtifact(BaseModel):
    """The ``workflow_template`` artifact. What the line uses is derived, never declared."""

    model_config = ConfigDict(extra="forbid")

    # The editor's and start's name rule; title and description fit their DB columns.
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4096)
    dsl_yaml: str

    @field_validator("dsl_yaml")
    @classmethod
    def _size(cls, value: str) -> str:
        if len(value.encode()) > WORKFLOW_TEMPLATE_MAX_BYTES:
            raise ValueError(f"dsl_yaml is over {WORKFLOW_TEMPLATE_MAX_BYTES} bytes")
        return value


def _errors(exc: ValidationError) -> str:
    # loc + msg only: pydantic's default text echoes the input, which may be 128 KiB.
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()
    )


def _strings(node: Any) -> Iterator[str]:
    """Every decoded string scalar and mapping key, each object once (aliases share objects)."""
    seen: set[int] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, str):
            yield item
        elif isinstance(item, bytes):  # !!binary: pydantic would decode it into a str
            yield item.decode("utf-8", "replace")
        elif isinstance(item, dict | list | tuple | set):
            stack.extend((x for kv in item.items() for x in kv) if isinstance(item, dict) else item)


class _UniqueKeyLoader(yaml.SafeLoader):
    """``safe_load`` that refuses a repeated key: YAML keeps the last value, so a scan
    of the decoded document never sees the earlier one, yet the stored text keeps it."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        keys = [
            self.construct_object(key, deep=True)
            for key, _ in node.value
            if key.tag != "tag:yaml.org,2002:merge"
        ]
        try:
            repeated = len(set(keys)) != len(keys)
        except TypeError:  # an unhashable key: the base constructor refuses it
            repeated = False
        if repeated:
            raise yaml.constructor.ConstructorError(
                None, None, "found a duplicate mapping key", node.start_mark
            )
        return super().construct_mapping(node, deep=deep)


def _param_keys(raw: dict[str, Any]) -> Iterator[str]:
    stations = raw.get("stations")
    for station in stations.values() if isinstance(stations, dict) else ():
        params = station.get("params") if isinstance(station, dict) else None
        if isinstance(params, dict):
            yield from map(str, params)


class WorkflowTemplateInstaller:
    """Validates a ``workflow_template`` artifact: a DSL v2 line (spec K §4.4).

    Declarative only: the YAML is ``safe_load``-ed and checked by the pure contract
    validators. Error messages start with a stable code.
    """

    kind = ArtifactKind.workflow_template
    target_kind = "workflow_definition"

    def __init__(self, secret_like: Callable[[str], bool] = default_secret_like) -> None:
        self._secret_like = secret_like

    def validate(self, artifact: dict) -> ValidatedArtifact:
        # 1. The strict artifact model. Its errors name unknown keys, so scan those first.
        if isinstance(artifact, dict) and any(self._secret_like(str(k)) for k in artifact):
            raise SchemaInvalid("secret_like_content")
        try:
            art = WorkflowTemplateArtifact.model_validate(artifact)
        except ValidationError as exc:
            # from None: the pydantic error reprs its input (a title may hold a secret).
            raise SchemaInvalid(f"artifact_invalid: {_errors(exc)}") from None

        # 2. Secrets: the raw text, title and description, then every decoded string
        #    (YAML escapes hide a token from the raw scan) and station params key names,
        #    all before any parse error could echo a value.
        if any(self._secret_like(t) for t in (art.dsl_yaml, art.title, art.description or "")):
            raise SchemaInvalid("secret_like_content")
        try:
            raw = yaml.load(art.dsl_yaml, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
        except yaml.YAMLError as exc:
            raise SchemaInvalid(f"invalid_yaml: {exc}") from exc
        except RecursionError:  # PyYAML composes recursively
            raise SchemaInvalid("invalid_yaml: nesting too deep") from None
        if any(self._secret_like(text) for text in _strings(raw)):
            raise SchemaInvalid("secret_like_content")
        if not isinstance(raw, dict):
            raise SchemaInvalid(f"not_a_line: dsl_yaml must be a mapping, got {type(raw).__name__}")
        if any(hint in key.lower() for key in _param_keys(raw) for hint in _SECRET_KEY_HINTS):
            raise SchemaInvalid("secret_like_content")

        # 3. A DSL v2 line whose workflow is the artifact's name. Never repr ``raw``:
        #    YAML aliases can make it exponentially large to print (not to load).
        if raw.get("dsl_version") != "2":
            raise SchemaInvalid("not_a_line: dsl_version must be '2'")
        try:
            line = parse_line(raw)
        except RecursionError:
            raise SchemaInvalid("line_parse_error: nesting too deep") from None
        except ValidationError as exc:
            # from None: the pydantic error's text reprs its input, so a logged
            # traceback would print the expanded aliases.
            raise SchemaInvalid(f"line_parse_error: {_errors(exc)}") from None
        if line.name != art.name:
            raise SchemaInvalid(
                f"name_mismatch: artifact name {art.name!r} != workflow {line.name!r}"
            )

        # 4. The graph rules.
        errors = validate_line(line)
        if errors:
            codes = ", ".join(dict.fromkeys(e.code.value for e in errors))
            raise SchemaInvalid(f"line_invalid: {codes}")

        # 5. Bundled names stay bundled.
        if art.name in RESERVED_LINE_NAMES:
            raise SchemaInvalid(f"reserved_name: {art.name!r} is a bundled definition")

        # 6. The preview: what the line uses, derived from the DSL.
        return ValidatedArtifact(
            target_kind=self.target_kind,
            resolved_config={
                "name": art.name,
                "title": art.title,
                "description": art.description,
                "dsl_yaml": art.dsl_yaml,
                "summary": _summary(line),
            },
            requires_admin_followup=_followups(line),
        )


def _summary(line: LineDefinition) -> dict[str, Any]:
    stations = line.stations.values()
    return {
        "agents": [s for s, st in line.stations.items() if isinstance(st, AgentStation)],
        "services": list(
            dict.fromkeys(st.service for st in stations if isinstance(st, ServiceStation))
        ),
        "human_gates": list(
            dict.fromkeys(st.gate.value for st in stations if isinstance(st, HumanStation))
        ),
        "budget": line.budget.model_dump(mode="json") if line.budget else None,
    }


def _followups(line: LineDefinition) -> list[str]:
    stations = line.stations.values()
    credentials = dict.fromkeys(
        cred
        for st in stations
        if isinstance(st, ServiceStation)
        and (cred := SERVICE_CATALOG[st.service].credential) is not None
    )
    profiles = dict.fromkeys(st.skill_profile for st in stations if isinstance(st, AgentStation))
    return [
        *(f"supply the {c!r} service credential" for c in credentials),
        *(f"provide skill profile {p!r} in the workspace" for p in profiles),
        *LINE_FOLLOWUPS,
    ]


AnyInstaller = McpConnectorInstaller | SkillProfileInstaller | WorkflowTemplateInstaller

#: The registered installers. ``policy_template`` is intentionally absent (F32 §12).
INSTALLERS: dict[ArtifactKind, AnyInstaller] = {
    ArtifactKind.mcp_connector: McpConnectorInstaller(),
    ArtifactKind.skill_profile: SkillProfileInstaller(),
    ArtifactKind.workflow_template: WorkflowTemplateInstaller(),
}


def get_installer(kind: ArtifactKind) -> AnyInstaller:
    """Return the installer for ``kind`` or raise :class:`UnknownArtifactKind`."""
    installer = INSTALLERS.get(kind)
    if installer is None:
        raise UnknownArtifactKind(
            f"no installer registered for kind '{kind.value}' (reserved — requires F04)"
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
    "LINE_FOLLOWUPS",
    "MCP_FOLLOWUPS",
    "RESERVED_LINE_NAMES",
    "WORKFLOW_TEMPLATE_MAX_BYTES",
    "McpConnectorInstaller",
    "SkillProfileInstaller",
    "ValidatedArtifact",
    "WorkflowTemplateArtifact",
    "WorkflowTemplateInstaller",
    "build_install_plan",
    "default_secret_like",
    "get_installer",
]
