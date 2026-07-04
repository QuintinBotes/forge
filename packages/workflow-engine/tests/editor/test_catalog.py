"""Catalog completeness tests (F28 AC 3)."""

from __future__ import annotations

import uuid

from forge_contracts.enums import ExecutionMode, WorkflowState
from forge_workflow.editor.catalog import RegistryCatalog


def test_catalog_includes_all_workflow_states(registry_catalog: RegistryCatalog) -> None:
    catalog = registry_catalog.build()
    for state in WorkflowState:
        assert state.value in catalog.states


def test_catalog_modes(registry_catalog: RegistryCatalog) -> None:
    catalog = registry_catalog.build()
    assert catalog.modes == [m.value for m in ExecutionMode]
    assert "single_agent" in catalog.modes
    assert "supervised_multi_agent" in catalog.modes


def test_catalog_preconditions(registry_catalog: RegistryCatalog) -> None:
    catalog = registry_catalog.build()
    names = {p.name for p in catalog.preconditions}
    assert {"repo_target_set", "policy_loaded", "skill_profile_set", "knowledge_synced"} <= names
    assert all(p.is_precondition for p in catalog.preconditions)


def test_catalog_guards_have_descriptions(registry_catalog: RegistryCatalog) -> None:
    catalog = registry_catalog.build()
    names = {g.name for g in catalog.guards}
    assert "retry_budget_remaining" in names
    assert "review_approved_by_human" in names
    assert all(not g.is_precondition for g in catalog.guards)


def test_catalog_effects_have_provided_by(registry_catalog: RegistryCatalog) -> None:
    catalog = registry_catalog.build()
    by_name = {e.name: e for e in catalog.effects}
    assert "start_agent_run" in by_name
    assert by_name["start_agent_run"].provided_by == "agent-runtime"


def test_catalog_events_include_triggers(registry_catalog: RegistryCatalog) -> None:
    catalog = registry_catalog.build()
    assert "spec_approved_by_human" in catalog.events
    assert "run_checks" in catalog.events


def test_catalog_skills_from_provider() -> None:
    catalog = RegistryCatalog(
        skill_names_provider=lambda _ws: ["custom-skill"]
    ).build(workspace_id=uuid.uuid4())
    assert "custom-skill" in catalog.skills
    # bundled skills are folded in too
    assert "spec-analyst" in catalog.skills
