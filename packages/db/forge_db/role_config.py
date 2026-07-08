"""SQL-backed :class:`~forge_contracts.orchestration_config.RoleConfigStore` (ao-config).

The canonical contract (DTOs, enums, the ``RoleConfigStore`` Protocol) lives in
``forge_contracts.orchestration_config``; :class:`AgentRoleConfig` in
``forge_db.models.role_config`` is the ORM row. This module is the repository
that reads/writes those rows and satisfies the Protocol structurally (it is
``runtime_checkable``, so ``isinstance(store, RoleConfigStore)`` holds for an
instance of this class).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from forge_contracts.orchestration_config import AgentRole, Effort, RoleConfigOverride
from forge_db.models.role_config import AgentRoleConfig

__all__ = ["SqlRoleConfigStore"]


def _to_dto(row: AgentRoleConfig) -> RoleConfigOverride:
    return RoleConfigOverride(
        id=row.id,
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        role=row.role,
        model_or_tier=row.model_or_tier,
        effort=row.effort,
    )


class SqlRoleConfigStore:
    """Session-scoped repository over ``agent_role_config``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_override(
        self, workspace_id: UUID, role: AgentRole, *, project_id: UUID | None = None
    ) -> RoleConfigOverride | None:
        row = self._session.scalars(
            select(AgentRoleConfig).where(
                AgentRoleConfig.workspace_id == workspace_id,
                AgentRoleConfig.project_id.is_(project_id)
                if project_id is None
                else AgentRoleConfig.project_id == project_id,
                AgentRoleConfig.role == role,
            )
        ).one_or_none()
        return None if row is None else _to_dto(row)

    def upsert_override(
        self,
        workspace_id: UUID,
        role: AgentRole,
        model_or_tier: str,
        effort: Effort,
        *,
        project_id: UUID | None = None,
    ) -> RoleConfigOverride:
        # Postgres-native upsert. The two scopes are backed by two *different*
        # unique indexes (see ``forge_db.models.role_config``), so which one
        # ``ON CONFLICT`` targets depends on whether this is a workspace-wide
        # (``project_id is None``) or project-scoped override.
        stmt = pg_insert(AgentRoleConfig).values(
            workspace_id=workspace_id,
            project_id=project_id,
            role=role,
            model_or_tier=model_or_tier,
            effort=effort,
        )
        if project_id is None:
            stmt = stmt.on_conflict_do_update(
                index_elements=["workspace_id", "role"],
                index_where=text("project_id IS NULL"),
                set_={"model_or_tier": model_or_tier, "effort": effort},
            )
        else:
            stmt = stmt.on_conflict_do_update(
                index_elements=["workspace_id", "project_id", "role"],
                set_={"model_or_tier": model_or_tier, "effort": effort},
            )
        self._session.execute(stmt)
        self._session.flush()
        found = self.get_override(workspace_id, role, project_id=project_id)
        if found is None:  # pragma: no cover - defensive, upsert above guarantees a row
            raise RuntimeError("upsert_override: row not found immediately after upsert")
        return found

    def delete_override(
        self, workspace_id: UUID, role: AgentRole, *, project_id: UUID | None = None
    ) -> bool:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(AgentRoleConfig).where(
                    AgentRoleConfig.workspace_id == workspace_id,
                    AgentRoleConfig.project_id.is_(project_id)
                    if project_id is None
                    else AgentRoleConfig.project_id == project_id,
                    AgentRoleConfig.role == role,
                )
            ),
        )
        self._session.flush()
        return bool(result.rowcount)

    def list_overrides(
        self, workspace_id: UUID, *, project_id: UUID | None = None
    ) -> list[RoleConfigOverride]:
        stmt = select(AgentRoleConfig).where(AgentRoleConfig.workspace_id == workspace_id)
        if project_id is not None:
            stmt = stmt.where(AgentRoleConfig.project_id == project_id)
        rows = self._session.scalars(stmt.order_by(AgentRoleConfig.role)).all()
        return [_to_dto(row) for row in rows]
