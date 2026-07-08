"""Adaptive Orchestration workspace-wide settings store (``ao-settings-api``).

Backs the storage boundary defined by
:class:`forge_contracts.orchestration_config.AoSettingsStore`: exactly one row
per workspace holding the auto-route toggle, the ``tier -> model`` overrides
layered onto the model router's defaults, and the complexity-score
thresholds. Distinct from ``agent_role_config`` (per-role overrides, multiple
rows per workspace) -- this is a singleton row per workspace, enforced by a
unique index on ``workspace_id`` alone.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, json_type


class AoWorkspaceSettings(WorkspaceScopedModel):
    """The single Adaptive Orchestration settings row for one workspace."""

    __tablename__ = "ao_workspace_settings"
    __table_args__ = (
        Index(
            "uq_ao_workspace_settings_workspace",
            "workspace_id",
            unique=True,
        ),
    )

    auto_route: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: ``{provider: {tier: model}}`` layered onto the model router's
    #: per-provider ``DEFAULT_TIER_MODELS``; empty dict means no override.
    tier_model_overrides: Mapped[dict[str, dict[str, str]]] = mapped_column(
        json_type(), nullable=False, default=dict
    )
    #: ``NULL`` = use the hardcoded ``forge_orchestration_policy.complexity``
    #: default threshold.
    junior_max: Mapped[int | None] = mapped_column(nullable=True)
    medior_max: Mapped[int | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"AoWorkspaceSettings(workspace_id={self.workspace_id!r}, "
            f"auto_route={self.auto_route!r}, junior_max={self.junior_max!r}, "
            f"medior_max={self.medior_max!r})"
        )


__all__ = ["AoWorkspaceSettings"]
