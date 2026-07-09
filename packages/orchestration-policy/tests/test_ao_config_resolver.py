"""Tests for the ao-config resolver (forge_orchestration_policy.role_config).

Pure unit tests against a fake in-memory :class:`RoleConfigStore` -- no
database. The real, SQL-backed store (``forge_db.role_config.SqlRoleConfigStore``)
is exercised against pgvector Postgres in ``packages/db/tests/test_role_config.py``,
which also drives this same resolver through the real store to prove the two
sides conform to the same Protocol.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from forge_contracts.orchestration_config import (
    DEFAULT_ROLE_CONFIG,
    AgentRole,
    Effort,
    RoleConfigOverride,
)
from forge_orchestration_policy import resolve_effective_config

WORKSPACE = uuid.uuid4()
PROJECT = uuid.uuid4()
OTHER_PROJECT = uuid.uuid4()


@dataclass
class FakeRoleConfigStore:
    """An in-memory double conforming to ``RoleConfigStore`` structurally."""

    _rows: dict[tuple[uuid.UUID, uuid.UUID | None, AgentRole], RoleConfigOverride] = field(
        default_factory=dict
    )

    def get_override(
        self, workspace_id: uuid.UUID, role: AgentRole, *, project_id: uuid.UUID | None = None
    ) -> RoleConfigOverride | None:
        return self._rows.get((workspace_id, project_id, role))

    def upsert_override(
        self,
        workspace_id: uuid.UUID,
        role: AgentRole,
        model_or_tier: str,
        effort: Effort,
        *,
        project_id: uuid.UUID | None = None,
    ) -> RoleConfigOverride:
        row = RoleConfigOverride(
            workspace_id=workspace_id,
            project_id=project_id,
            role=role,
            model_or_tier=model_or_tier,
            effort=effort,
        )
        self._rows[(workspace_id, project_id, role)] = row
        return row

    def delete_override(
        self, workspace_id: uuid.UUID, role: AgentRole, *, project_id: uuid.UUID | None = None
    ) -> bool:
        return self._rows.pop((workspace_id, project_id, role), None) is not None

    def list_overrides(
        self, workspace_id: uuid.UUID, *, project_id: uuid.UUID | None = None
    ) -> list[RoleConfigOverride]:
        return [
            row
            for (ws, proj, _role), row in self._rows.items()
            if ws == workspace_id and (project_id is None or proj == project_id)
        ]


def test_falls_back_to_hardcoded_default_when_no_override() -> None:
    store = FakeRoleConfigStore()
    resolved = resolve_effective_config(store, WORKSPACE, AgentRole.CODER)
    default = DEFAULT_ROLE_CONFIG[AgentRole.CODER]
    assert resolved.source == "default"
    assert resolved.model_or_tier == default.model_or_tier
    assert resolved.effort == default.effort
    assert resolved.role == AgentRole.CODER


def test_workspace_override_beats_default() -> None:
    store = FakeRoleConfigStore()
    store.upsert_override(WORKSPACE, AgentRole.CODER, "senior", Effort.MAX)
    resolved = resolve_effective_config(store, WORKSPACE, AgentRole.CODER)
    assert resolved.source == "workspace"
    assert resolved.model_or_tier == "senior"
    assert resolved.effort == Effort.MAX


def test_project_override_beats_workspace_override() -> None:
    store = FakeRoleConfigStore()
    store.upsert_override(WORKSPACE, AgentRole.REVIEWER, "senior", Effort.MEDIUM)
    store.upsert_override(
        WORKSPACE, AgentRole.REVIEWER, "claude-opus-4-6", Effort.MAX, project_id=PROJECT
    )

    resolved = resolve_effective_config(store, WORKSPACE, AgentRole.REVIEWER, project_id=PROJECT)
    assert resolved.source == "project"
    assert resolved.model_or_tier == "claude-opus-4-6"
    assert resolved.effort == Effort.MAX

    # A different project in the same workspace still falls through to the
    # workspace-wide override, not the project-scoped one above.
    other = resolve_effective_config(store, WORKSPACE, AgentRole.REVIEWER, project_id=OTHER_PROJECT)
    assert other.source == "workspace"
    assert other.model_or_tier == "senior"
    assert other.effort == Effort.MEDIUM


def test_no_project_override_falls_back_to_workspace_when_project_id_given() -> None:
    store = FakeRoleConfigStore()
    store.upsert_override(WORKSPACE, AgentRole.PLANNER, "medior", Effort.LOW)
    resolved = resolve_effective_config(store, WORKSPACE, AgentRole.PLANNER, project_id=PROJECT)
    assert resolved.source == "workspace"
    assert resolved.model_or_tier == "medior"
    assert resolved.effort == Effort.LOW


def test_deleting_override_reverts_to_next_fallback() -> None:
    store = FakeRoleConfigStore()
    store.upsert_override(WORKSPACE, AgentRole.SPEC_AUTHOR, "senior", Effort.HIGH)
    assert store.delete_override(WORKSPACE, AgentRole.SPEC_AUTHOR) is True
    resolved = resolve_effective_config(store, WORKSPACE, AgentRole.SPEC_AUTHOR)
    assert resolved.source == "default"


def test_all_roles_resolve_to_a_default() -> None:
    store = FakeRoleConfigStore()
    for role in AgentRole:
        resolved = resolve_effective_config(store, WORKSPACE, role)
        assert resolved.role == role
        assert resolved.source == "default"
