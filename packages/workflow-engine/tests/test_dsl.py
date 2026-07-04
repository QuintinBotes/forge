"""Tests for the workflow DSL parser (plan Task 1.8).

The DSL parser turns the spec's YAML workflow definition into a validated
``WorkflowDefinition`` contract DTO. These tests pin:

- the spec's ``default_feature`` DSL parses verbatim (``workflow:`` -> ``name``,
  ``from``/``to`` aliases, retry + escalation policy),
- parsing accepts either a YAML string or a filesystem path,
- a structurally invalid definition raises ``WorkflowDefinitionError`` (never a
  bare pydantic / yaml error).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_contracts import WorkflowDefinition
from forge_workflow.default_workflow import DEFAULT_FEATURE_WORKFLOW_YAML
from forge_workflow.dsl import load_definition, parse_definition
from forge_workflow.exceptions import WorkflowDefinitionError


def test_parses_spec_dsl_top_level_fields() -> None:
    definition = parse_definition(DEFAULT_FEATURE_WORKFLOW_YAML)
    assert isinstance(definition, WorkflowDefinition)
    # ``workflow:`` in the DSL maps onto the DTO ``name`` field.
    assert definition.name == "default_feature"
    assert definition.version == "1"
    assert definition.modes["default"] == "single_agent"
    assert "supervised_multi_agent" in definition.modes["optional"]


def test_parses_retry_and_escalation_policy() -> None:
    definition = parse_definition(DEFAULT_FEATURE_WORKFLOW_YAML)
    assert definition.retry_policy.max_retries == 3
    assert definition.retry_policy.backoff == "exponential"
    assert definition.retry_policy.initial_delay_seconds == 30
    assert definition.escalation_policy.confidence_threshold == 0.72
    assert definition.escalation_policy.on_low_confidence == "pause_and_notify"
    assert definition.escalation_policy.on_policy_conflict == "escalate_to_admin"


def test_transitions_use_from_to_aliases() -> None:
    definition = parse_definition(DEFAULT_FEATURE_WORKFLOW_YAML)
    first = definition.transitions[0]
    assert first.from_state == "created"
    assert first.to_state == "spec_drafting"
    assert first.action == "generate_spec_draft"
    assert first.skill == "spec-analyst"


def test_list_valued_when_is_preserved() -> None:
    definition = parse_definition(DEFAULT_FEATURE_WORKFLOW_YAML)
    merged = [
        t
        for t in definition.transitions
        if t.from_state == "awaiting_review" and t.to_state == "merged"
    ]
    assert len(merged) == 1
    assert merged[0].when == ["review_approved_by_human", "ci_status_green", "spec_validated"]


def test_load_definition_from_path(tmp_path: Path) -> None:
    target = tmp_path / "wf.yaml"
    target.write_text(DEFAULT_FEATURE_WORKFLOW_YAML)
    # As a Path object...
    definition = load_definition(target)
    assert definition.name == "default_feature"
    # ...and as a string path.
    definition2 = load_definition(str(target))
    assert definition2.name == "default_feature"


def test_load_definition_from_yaml_string() -> None:
    definition = load_definition(DEFAULT_FEATURE_WORKFLOW_YAML)
    assert definition.name == "default_feature"


def test_load_definition_validates_graph() -> None:
    # A definition with no transitions is structurally invalid.
    with pytest.raises(WorkflowDefinitionError):
        load_definition("workflow: empty\nversion: '1'\ntransitions: []\n")


def test_malformed_transition_raises_definition_error() -> None:
    # Missing the required ``to`` key -> WorkflowDefinitionError (not ValidationError).
    bad = "workflow: bad\ntransitions:\n  - from: created\n"
    with pytest.raises(WorkflowDefinitionError):
        load_definition(bad)


def test_non_mapping_yaml_raises_definition_error() -> None:
    with pytest.raises(WorkflowDefinitionError):
        load_definition("- just\n- a\n- list\n")
