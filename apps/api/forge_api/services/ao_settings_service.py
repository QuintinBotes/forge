"""Service layer for the Adaptive Orchestration settings API (``ao-settings-api``).

Wires the storage boundaries the DB/policy layers already define —
:class:`~forge_contracts.orchestration_config.RoleConfigStore` +
:func:`~forge_orchestration_policy.resolve_effective_config` for per-role
config, and :class:`~forge_contracts.orchestration_config.AoSettingsStore` for
the workspace-wide auto-route toggle / tier-model map / complexity thresholds
— plus the :class:`~forge_orchestration_policy.complexity` scorer and the
:mod:`forge_agent.providers.router` model router, for the routing-preview
endpoint. No new persistence logic lives here; it composes existing seams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from forge_orchestration_policy import Strategy, Tier
from forge_orchestration_policy.complexity import _JUNIOR_MAX as _DEFAULT_JUNIOR_MAX
from forge_orchestration_policy.complexity import _MEDIOR_MAX as _DEFAULT_MEDIOR_MAX
from forge_orchestration_policy.complexity import (
    BlastRadiusLevel,
    SizingSignals,
    score_complexity,
)
from forge_orchestration_policy.role_config import resolve_effective_config

from forge_agent.providers.config import ProviderName
from forge_agent.providers.router import ModelRouter
from forge_contracts.enums import Priority, TaskKind
from forge_contracts.orchestration_config import (
    AgentRole,
    AoSettingsStore,
    AoWorkspaceSettingsDTO,
    EffectiveRoleConfig,
    Effort,
    RoleConfigStore,
)

__all__ = ["AoSettingsService", "EffectiveAoSettings", "RoutingPreview"]

_VALID_BLAST_RADIUS: frozenset[str] = frozenset({"low", "medium", "high"})
_VALID_TIERS: tuple[Tier, ...] = ("junior", "medior", "senior")


def _validate_blast_radius(value: str | None) -> BlastRadiusLevel | None:
    """Narrow an arbitrary string to :data:`BlastRadiusLevel`, or raise."""
    if value is None:
        return None
    if value not in _VALID_BLAST_RADIUS:
        raise ValueError(f"invalid blast_radius: {value!r}")
    return value  # type: ignore[return-value]


def _validated_tier_models(overrides: dict[str, str]) -> dict[Tier, str]:
    """Keep only the well-known tier keys from a JSON-sourced override map."""
    return {tier: model for tier, model in overrides.items() if tier in _VALID_TIERS}


def _tier_for_score(score: int, junior_max: int, medior_max: int) -> Tier:
    if score <= junior_max:
        return "junior"
    if score <= medior_max:
        return "medior"
    return "senior"


def _strategy_for(tier: Tier, signals: SizingSignals) -> Strategy:
    if tier == "senior":
        return "swarm"
    if signals.repo_count > 1:
        return "swarm"
    if signals.touches_contracts and signals.touches_security:
        return "swarm"
    return "single"


@dataclass(frozen=True)
class EffectiveAoSettings:
    """The resolved workspace settings with defaults filled in (not a DTO —
    a small carrier the router serializes into ``AoSettingsOut``)."""

    workspace_id: UUID
    auto_route: bool
    tier_model_overrides: dict[str, dict[str, str]]
    junior_max: int
    medior_max: int
    junior_max_is_default: bool
    medior_max_is_default: bool


@dataclass(frozen=True)
class RoutingPreview:
    """The computed routing-preview result (carrier, not a persisted DTO)."""

    tier: Tier
    strategy: Strategy
    score: int
    reasons: list[str] = field(default_factory=list)
    model: str = ""
    provider: ProviderName = ProviderName.anthropic
    junior_max: int = _DEFAULT_JUNIOR_MAX
    medior_max: int = _DEFAULT_MEDIOR_MAX
    auto_route_enabled: bool = True


class AoSettingsService:
    """Reads/writes per-role config + workspace-wide Adaptive Orchestration settings."""

    def __init__(self, role_store: RoleConfigStore, settings_store: AoSettingsStore) -> None:
        self._role_store = role_store
        self._settings_store = settings_store

    # --- per-role model+effort config --------------------------------- #

    def list_role_configs(
        self, workspace_id: UUID, *, project_id: UUID | None = None
    ) -> list[EffectiveRoleConfig]:
        return [
            resolve_effective_config(self._role_store, workspace_id, role, project_id=project_id)
            for role in AgentRole
        ]

    def upsert_role_config(
        self,
        workspace_id: UUID,
        role: AgentRole,
        model_or_tier: str,
        effort: Effort,
        *,
        project_id: UUID | None = None,
    ) -> EffectiveRoleConfig:
        self._role_store.upsert_override(
            workspace_id, role, model_or_tier, effort, project_id=project_id
        )
        return resolve_effective_config(self._role_store, workspace_id, role, project_id=project_id)

    def delete_role_config(
        self, workspace_id: UUID, role: AgentRole, *, project_id: UUID | None = None
    ) -> EffectiveRoleConfig:
        self._role_store.delete_override(workspace_id, role, project_id=project_id)
        return resolve_effective_config(self._role_store, workspace_id, role, project_id=project_id)

    # --- workspace-wide settings ---------------------------------------- #

    def get_settings(self, workspace_id: UUID) -> EffectiveAoSettings:
        return self._effective(workspace_id, self._settings_store.get_settings(workspace_id))

    def update_settings(
        self,
        workspace_id: UUID,
        *,
        auto_route: bool | None = None,
        tier_model_overrides: dict[str, dict[str, str]] | None = None,
        junior_max: int | None = None,
        medior_max: int | None = None,
        clear_junior_max: bool = False,
        clear_medior_max: bool = False,
    ) -> EffectiveAoSettings:
        raw = self._settings_store.upsert_settings(
            workspace_id,
            auto_route=auto_route,
            tier_model_overrides=tier_model_overrides,
            junior_max=junior_max,
            medior_max=medior_max,
            clear_junior_max=clear_junior_max,
            clear_medior_max=clear_medior_max,
        )
        return self._effective(workspace_id, raw)

    @staticmethod
    def _effective(workspace_id: UUID, raw: AoWorkspaceSettingsDTO | None) -> EffectiveAoSettings:
        if raw is None:
            return EffectiveAoSettings(
                workspace_id=workspace_id,
                auto_route=True,
                tier_model_overrides={},
                junior_max=_DEFAULT_JUNIOR_MAX,
                medior_max=_DEFAULT_MEDIOR_MAX,
                junior_max_is_default=True,
                medior_max_is_default=True,
            )
        junior_max = raw.junior_max
        medior_max = raw.medior_max
        return EffectiveAoSettings(
            workspace_id=workspace_id,
            auto_route=raw.auto_route,
            tier_model_overrides=raw.tier_model_overrides,
            junior_max=_DEFAULT_JUNIOR_MAX if junior_max is None else junior_max,
            medior_max=_DEFAULT_MEDIOR_MAX if medior_max is None else medior_max,
            junior_max_is_default=junior_max is None,
            medior_max_is_default=medior_max is None,
        )

    # --- routing preview -------------------------------------------------- #

    def preview_routing(
        self,
        workspace_id: UUID,
        *,
        kind: str,
        priority: str,
        blast_radius: str | None,
        file_count: int,
        repo_count: int,
        requirement_count: int,
        acceptance_criteria_count: int,
        touches_contracts: bool,
        touches_security: bool,
        dependency_count: int,
        open_questions_count: int,
        underspecified: bool,
        provider: ProviderName,
    ) -> RoutingPreview:
        signals = SizingSignals(
            kind=TaskKind(kind),
            priority=Priority(priority),
            blast_radius=_validate_blast_radius(blast_radius),
            file_count=file_count,
            repo_count=repo_count,
            requirement_count=requirement_count,
            acceptance_criteria_count=acceptance_criteria_count,
            touches_contracts=touches_contracts,
            touches_security=touches_security,
            dependency_count=dependency_count,
            open_questions_count=open_questions_count,
            underspecified=underspecified,
        )
        sizing = score_complexity(signals)
        settings = self.get_settings(workspace_id)

        if settings.junior_max != _DEFAULT_JUNIOR_MAX or settings.medior_max != _DEFAULT_MEDIOR_MAX:
            tier = _tier_for_score(sizing.score, settings.junior_max, settings.medior_max)
            strategy = _strategy_for(tier, signals)
            reasons = [
                *sizing.reasons,
                f"workspace thresholds (junior_max={settings.junior_max}, "
                f"medior_max={settings.medior_max}) -> tier={tier}",
            ]
        else:
            tier = sizing.tier
            strategy = sizing.strategy
            reasons = sizing.reasons

        provider_overrides = _validated_tier_models(
            settings.tier_model_overrides.get(provider.value, {})
        )
        router = ModelRouter(provider=provider, tier_models=provider_overrides)
        model = router.resolve(tier)

        return RoutingPreview(
            tier=tier,
            strategy=strategy,
            score=sizing.score,
            reasons=reasons,
            model=model,
            provider=provider,
            junior_max=settings.junior_max,
            medior_max=settings.medior_max,
            auto_route_enabled=settings.auto_route,
        )
