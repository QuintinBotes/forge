"""Spec K §4.1: ``parse_definition`` dispatches on ``dsl_version``.

Absent, null, "1" or 1 keeps today's ``WorkflowDefinition`` (and refuses v2 keys); "2"
returns a ``LineDefinition``; anything else is an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_contracts import LineDefinition, WorkflowDefinition, validate_line
from forge_contracts.lines import FEATURE_INVARIANTS as LINE_FEATURE_INVARIANTS
from forge_workflow.default_workflow import DEFAULT_FEATURE_WORKFLOW_YAML
from forge_workflow.dsl import parse_definition
from forge_workflow.editor.validation import FEATURE_INVARIANTS
from forge_workflow.exceptions import WorkflowDefinitionError

IMPLEMENTATION_LOOP = (
    Path(__file__).resolve().parents[2] / "contracts/tests/fixtures/implementation_loop.yaml"
)
V1 = "workflow: tiny\ntransitions:\n  - {from: created, to: done, when: go}\n"


def test_v2_returns_a_line_that_validates() -> None:
    line = parse_definition(IMPLEMENTATION_LOOP.read_text())
    assert isinstance(line, LineDefinition)
    assert line.name == "implementation_loop"
    assert validate_line(line) == []


@pytest.mark.parametrize("version", ['"1"', "1", "null"])
def test_absent_and_explicit_v1_return_the_same_workflow_definition(version: str) -> None:
    absent = parse_definition(DEFAULT_FEATURE_WORKFLOW_YAML)
    explicit = parse_definition(f"dsl_version: {version}\n" + DEFAULT_FEATURE_WORKFLOW_YAML)
    assert type(absent) is WorkflowDefinition
    assert type(explicit) is WorkflowDefinition
    assert absent == explicit


@pytest.mark.parametrize(
    "extra",
    ["stations: {}", "terminals: {done: succeeded}", "budget: {max_tokens: 1}", "initial: created"],
)
def test_v1_carrying_v2_keys_is_refused(extra: str) -> None:
    key = extra.split(":")[0]
    with pytest.raises(WorkflowDefinitionError, match=key):
        parse_definition(V1 + extra + "\n")


@pytest.mark.parametrize("version", ['"3"', "2", "true", "1.0", '"v1"', "[1]"])
def test_unknown_dsl_version_is_refused(version: str) -> None:
    with pytest.raises(WorkflowDefinitionError, match="unsupported dsl_version"):
        parse_definition(f"dsl_version: {version}\n" + V1)


def test_v2_shape_error_is_a_definition_error() -> None:
    with pytest.raises(WorkflowDefinitionError, match="invalid line definition"):
        parse_definition('dsl_version: "2"\n' + V1)


def test_contracts_feature_invariants_match_the_editor() -> None:
    def shape(inv: object) -> tuple[object, ...]:
        fields = ("id", "description", "from_state", "to_state", "on_event", "required_signal")
        return tuple(getattr(inv, f) for f in fields)

    assert [shape(i) for i in LINE_FEATURE_INVARIANTS] == [shape(i) for i in FEATURE_INVARIANTS]
