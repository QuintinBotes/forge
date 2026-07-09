"""Tests for the F18 PM-adapter contract surface (forge_contracts.pm)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import forge_contracts.pm as pm
from forge_contracts import Direction


def test_module_exports_resolve() -> None:
    for name in pm.__all__:
        assert hasattr(pm, name), f"__all__ lists missing symbol: {name}"


def test_direction_is_reused_from_frozen_enums() -> None:
    # The v2 module reuses the frozen Direction enum verbatim.
    assert pm.Direction is Direction
    assert {d.value for d in pm.Direction} == {"in", "out"}


def test_provider_enum_values() -> None:
    assert {p.value for p in pm.PMProvider} == {
        "jira",
        "linear",
        "asana",
        "monday",
        "github_projects",
    }


def test_status_categories_match_spec() -> None:
    assert {c.value for c in pm.StatusCategory} == {
        "backlog",
        "unstarted",
        "started",
        "completed",
        "canceled",
    }


def test_forge_priority_values() -> None:
    assert {p.value for p in pm.ForgePriority} == {
        "none",
        "low",
        "medium",
        "high",
        "urgent",
    }


def test_external_task_roundtrip() -> None:
    t = pm.ExternalTask(
        provider=pm.PMProvider.jira,
        external_id="10001",
        external_key="ENG-1",
        url="https://acme.atlassian.net/browse/ENG-1",
        title="Do the thing",
        status_name="In Progress",
        status_category=pm.StatusCategory.started,
        external_updated_at=datetime.now(UTC),
    )
    assert t.labels == []
    assert pm.ExternalTask.model_validate(t.model_dump()) == t


def test_forge_task_requires_category_and_priority() -> None:
    t = pm.ForgeTask(
        id=uuid4(),
        key="TASK-1",
        project_id=uuid4(),
        title="Title",
        status_category=pm.StatusCategory.started,
        priority=pm.ForgePriority.high,
        updated_at=datetime.now(UTC),
    )
    assert t.version == 0


def test_connection_config_defaults() -> None:
    cfg = pm.PMConnectionConfig(
        provider=pm.PMProvider.linear,
        name="Eng Linear",
        project_id=uuid4(),
        external_project_key="ENG",
    )
    assert cfg.auth_type == "oauth"
    assert cfg.sync_direction is pm.SyncDirection.bidirectional
    assert cfg.conflict_policy is pm.ConflictPolicy.newest_wins
    assert cfg.on_external_delete == "unlink"


def test_pm_adapter_is_runtime_checkable_protocol() -> None:
    # An object missing the surface is not an instance.
    assert not isinstance(object(), pm.PMAdapter)
