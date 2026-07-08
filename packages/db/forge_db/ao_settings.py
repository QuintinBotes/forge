"""SQL-backed :class:`~forge_contracts.orchestration_config.AoSettingsStore` (ao-settings-api).

The canonical contract (the DTO + the ``AoSettingsStore`` Protocol) lives in
``forge_contracts.orchestration_config``; :class:`AoWorkspaceSettings` (ORM) in
``forge_db.models.ao_settings`` is the row. This module is the repository that
reads/writes it and satisfies the Protocol structurally (``runtime_checkable``,
so ``isinstance(store, AoSettingsStore)`` holds for an instance of this class).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from forge_contracts.orchestration_config import AoWorkspaceSettingsDTO
from forge_db.models.ao_settings import AoWorkspaceSettings

__all__ = ["SqlAoSettingsStore"]


def _to_dto(row: AoWorkspaceSettings) -> AoWorkspaceSettingsDTO:
    return AoWorkspaceSettingsDTO(
        workspace_id=row.workspace_id,
        auto_route=row.auto_route,
        tier_model_overrides=row.tier_model_overrides,
        junior_max=row.junior_max,
        medior_max=row.medior_max,
    )


class SqlAoSettingsStore:
    """Session-scoped repository over the one-row-per-workspace ``ao_workspace_settings``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_settings(self, workspace_id: UUID) -> AoWorkspaceSettingsDTO | None:
        row = self._session.scalars(
            select(AoWorkspaceSettings).where(AoWorkspaceSettings.workspace_id == workspace_id)
        ).one_or_none()
        return None if row is None else _to_dto(row)

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
        existing = self._session.scalars(
            select(AoWorkspaceSettings).where(AoWorkspaceSettings.workspace_id == workspace_id)
        ).one_or_none()

        if existing is None:
            stmt = pg_insert(AoWorkspaceSettings).values(
                workspace_id=workspace_id,
                auto_route=True if auto_route is None else auto_route,
                tier_model_overrides=tier_model_overrides or {},
                junior_max=None if clear_junior_max else junior_max,
                medior_max=None if clear_medior_max else medior_max,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["workspace_id"])
            self._session.execute(stmt)
            self._session.flush()
        else:
            if auto_route is not None:
                existing.auto_route = auto_route
            if tier_model_overrides is not None:
                existing.tier_model_overrides = tier_model_overrides
            if clear_junior_max:
                existing.junior_max = None
            elif junior_max is not None:
                existing.junior_max = junior_max
            if clear_medior_max:
                existing.medior_max = None
            elif medior_max is not None:
                existing.medior_max = medior_max
            self._session.flush()

        found = self.get_settings(workspace_id)
        if found is None:  # pragma: no cover - defensive, upsert above guarantees a row
            raise RuntimeError("upsert_settings: row not found immediately after upsert")
        return found
