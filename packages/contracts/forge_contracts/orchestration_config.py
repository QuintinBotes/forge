"""Per-role model+effort configuration contract (Adaptive Orchestration: ao-config).

This is an **additive** extension of the frozen ``forge_contracts`` surface (the
same pattern :mod:`forge_contracts.pm` uses): the DTOs, enums, and the
:class:`RoleConfigStore` Protocol live in their own module namespace and do
**not** mutate the frozen top-level ``__all__``.

Adaptive Orchestration configures five roles independently (planner, coder,
reviewer, spec_author, coordinator), each with a ``model_or_tier`` + an
``effort`` (low/medium/high/max). ``model_or_tier`` holds either a *tier*
keyword (``junior``/``medior``/``senior`` -- the same vocabulary
:mod:`forge_orchestration_policy.complexity` sizes tasks into, left for the
separate model router to resolve to a concrete provider model) or a concrete
model id a human override pins verbatim (e.g. ``claude-opus-4-6``).

Two scopes may override the hardcoded :data:`DEFAULT_ROLE_CONFIG`: a
workspace-wide override (``project_id`` is ``None``) and a project-scoped
override (``project_id`` set) that takes precedence over it. Merging
defaults + overrides is the resolver's job
(:func:`forge_orchestration_policy.role_config.resolve_effective_config`); this
module only defines the storage boundary (:class:`RoleConfigStore`) the
resolver reads from and :mod:`forge_db` implements.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = [
    "DEFAULT_ROLE_CONFIG",
    "AgentRole",
    "AoSettingsStore",
    "AoWorkspaceSettingsDTO",
    "EffectiveRoleConfig",
    "Effort",
    "RoleConfigOverride",
    "RoleConfigSource",
    "RoleConfigStore",
    "RoleModelConfig",
]


class AgentRole(StrEnum):
    """The five Adaptive Orchestration roles configured independently."""

    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    SPEC_AUTHOR = "spec_author"
    COORDINATOR = "coordinator"


class Effort(StrEnum):
    """Model "thinking effort" a role runs at (provider-agnostic)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


#: Where an :class:`EffectiveRoleConfig` came from, in resolution order
#: (project overrides workspace overrides the hardcoded default).
RoleConfigSource = Literal["default", "workspace", "project"]


class RoleModelConfig(BaseModel):
    """The ``{model_or_tier, effort}`` pair every default and override carries."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    model_or_tier: str
    effort: Effort


class RoleConfigOverride(RoleModelConfig):
    """A persisted workspace- or project-scoped override row.

    ``project_id is None`` means this is the workspace-wide override for
    ``role``; a non-``None`` ``project_id`` scopes it to that one project
    (and takes precedence over the workspace-wide row when both exist).
    """

    id: UUID | None = None
    workspace_id: UUID
    project_id: UUID | None = None
    role: AgentRole


class EffectiveRoleConfig(RoleModelConfig):
    """The resolved config for one role after merging defaults + overrides."""

    role: AgentRole
    source: RoleConfigSource


#: Sane, hardcoded per-role defaults (spec: "with sane defaults"). Planner,
#: reviewer, and coordinator default to the senior tier at high effort --
#: getting the plan, the review, and the swarm supervision right matters more
#: than saving cost on those three roles -- while coder and spec_author default
#: to the medior tier at medium effort (the common case; escalated to senior by
#: the (separate) Adaptive Orchestration sizing policy for complex work).
DEFAULT_ROLE_CONFIG: dict[AgentRole, RoleModelConfig] = {
    AgentRole.PLANNER: RoleModelConfig(model_or_tier="senior", effort=Effort.HIGH),
    AgentRole.CODER: RoleModelConfig(model_or_tier="medior", effort=Effort.MEDIUM),
    AgentRole.REVIEWER: RoleModelConfig(model_or_tier="senior", effort=Effort.HIGH),
    AgentRole.SPEC_AUTHOR: RoleModelConfig(model_or_tier="medior", effort=Effort.MEDIUM),
    AgentRole.COORDINATOR: RoleModelConfig(model_or_tier="senior", effort=Effort.HIGH),
}


@runtime_checkable
class RoleConfigStore(Protocol):
    """Storage boundary for per-role model+effort overrides (workspace/project scoped).

    Pure CRUD over override rows -- no default-merging here; that is the
    resolver's job (:func:`forge_orchestration_policy.role_config.resolve_effective_config`),
    which reads through this Protocol.
    """

    def get_override(
        self, workspace_id: UUID, role: AgentRole, *, project_id: UUID | None = None
    ) -> RoleConfigOverride | None:
        """The override row for ``(workspace_id, project_id, role)``, or ``None``."""
        ...

    def upsert_override(
        self,
        workspace_id: UUID,
        role: AgentRole,
        model_or_tier: str,
        effort: Effort,
        *,
        project_id: UUID | None = None,
    ) -> RoleConfigOverride:
        """Create or replace the override for ``(workspace_id, project_id, role)``."""
        ...

    def delete_override(
        self, workspace_id: UUID, role: AgentRole, *, project_id: UUID | None = None
    ) -> bool:
        """Remove the override, if any; ``True`` iff a row was deleted."""
        ...

    def list_overrides(
        self, workspace_id: UUID, *, project_id: UUID | None = None
    ) -> list[RoleConfigOverride]:
        """All overrides for ``workspace_id`` (optionally narrowed to one project)."""
        ...


class AoWorkspaceSettingsDTO(BaseModel):
    """The workspace-wide Adaptive Orchestration settings row (``ao-settings-api``).

    Distinct from :class:`RoleConfigOverride` (per-role, per-scope): this is the
    single workspace-wide row holding the ``auto_route`` toggle, the
    ``tier -> model`` overrides layered onto the model router's per-provider
    defaults (``{provider: {tier: model}}``), and the complexity-score
    thresholds a workspace may retune. ``junior_max``/``medior_max`` are
    ``None`` when unset (the hardcoded
    :mod:`forge_orchestration_policy.complexity` default applies) -- only the
    resolving service fills in the effective value.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    workspace_id: UUID
    auto_route: bool = True
    tier_model_overrides: dict[str, dict[str, str]] = {}
    junior_max: int | None = None
    medior_max: int | None = None


@runtime_checkable
class AoSettingsStore(Protocol):
    """Storage boundary for the one-row-per-workspace Adaptive Orchestration settings.

    Pure read/upsert over the single settings row -- default-filling (the
    hardcoded thresholds, ``auto_route=True`` when no row exists at all) is the
    service's job, not this Protocol's.
    """

    def get_settings(self, workspace_id: UUID) -> AoWorkspaceSettingsDTO | None:
        """The settings row for ``workspace_id``, or ``None`` when never set."""
        ...

    def upsert_settings(
        self,
        workspace_id: UUID,
        *,
        auto_route: bool | None = None,
        tier_model_overrides: dict[str, dict[str, str]] | None = None,
        junior_max: int | None = None,
        medior_max: int | None = None,
        clear_junior_max: bool = False,
        clear_medior_max: bool = False,
    ) -> AoWorkspaceSettingsDTO:
        """Create or partially update the settings row for ``workspace_id``.

        Every keyword defaults to "leave unchanged" (``None``) except the two
        explicit ``clear_*`` flags, which reset the corresponding threshold back
        to the hardcoded default -- setting a field to ``None`` cannot itself
        mean "clear" since ``None`` also means "leave unchanged".
        """
        ...
