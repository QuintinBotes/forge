"""Tests for the ao-config contract surface (forge_contracts.orchestration_config)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

import forge_contracts.orchestration_config as oc


def test_module_exports_resolve() -> None:
    for name in oc.__all__:
        assert hasattr(oc, name), f"__all__ lists missing symbol: {name}"


def test_agent_role_values() -> None:
    assert {r.value for r in oc.AgentRole} == {
        "planner",
        "coder",
        "reviewer",
        "spec_author",
        "coordinator",
    }


def test_effort_values() -> None:
    assert {e.value for e in oc.Effort} == {"low", "medium", "high", "max"}


def test_default_role_config_covers_every_role() -> None:
    assert set(oc.DEFAULT_ROLE_CONFIG) == set(oc.AgentRole)
    for config in oc.DEFAULT_ROLE_CONFIG.values():
        assert config.model_or_tier in {"junior", "medior", "senior"}
        assert isinstance(config.effort, oc.Effort)


def test_role_config_store_is_runtime_checkable_protocol() -> None:
    assert getattr(oc.RoleConfigStore, "_is_protocol", False)
    assert getattr(oc.RoleConfigStore, "_is_runtime_protocol", False)


def test_role_config_override_round_trip() -> None:
    ws = uuid.uuid4()
    project = uuid.uuid4()
    override = oc.RoleConfigOverride(
        workspace_id=ws,
        project_id=project,
        role=oc.AgentRole.CODER,
        model_or_tier="claude-opus-4-6",
        effort=oc.Effort.MAX,
    )
    assert oc.RoleConfigOverride.model_validate(override.model_dump()) == override


def test_effective_role_config_requires_source() -> None:
    with pytest.raises(ValidationError):
        oc.EffectiveRoleConfig(
            role=oc.AgentRole.PLANNER, model_or_tier="senior", effort=oc.Effort.HIGH
        )
