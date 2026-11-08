"""Shared fixtures for the F28 editor unit tests."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from forge_workflow.default_workflow import default_feature_definition
from forge_workflow.editor.catalog import RegistryCatalog, Vocabulary
from forge_workflow.editor.graph import WorkflowGraph, definition_to_graph


@pytest.fixture
def registry_catalog() -> RegistryCatalog:
    return RegistryCatalog()


@pytest.fixture
def vocabulary(registry_catalog: RegistryCatalog) -> Vocabulary:
    return registry_catalog.vocabulary


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
def bundled_default_graph() -> WorkflowGraph:
    return definition_to_graph(default_feature_definition(), title="Default Feature")


@pytest.fixture
def make_graph(
    bundled_default_graph: WorkflowGraph,
) -> Callable[..., WorkflowGraph]:
    """Return a factory cloning the bundled graph for mutation in tests."""

    def _factory() -> WorkflowGraph:
        return bundled_default_graph.model_copy(deep=True)

    return _factory


#: Spec K §4.5's first line (the P1a contracts fixture).
IMPLEMENTATION_LOOP = (
    Path(__file__).resolve().parents[3] / "contracts/tests/fixtures/implementation_loop.yaml"
).read_text()

#: A small valid line with one station of each kind, non-default fields set.
REVIEW_LINE = """
workflow: review_line
version: "3"
dsl_version: "2"
initial: draft
terminals: {shipped: succeeded}
budget: {max_tokens: 1000, max_usd: 2.5}
stations:
  draft:
    kind: agent
    role: implementer
    skill_profile: implementer
    context_template: default
    objective: "Draft {{task.title}}"
    max_visits: 4
  checks:
    {kind: service, service: forge.run_checks, timeout_seconds: 600, retry: {max_attempts: 2}}
  review:
    {kind: human, gate: pr, sla_seconds: 3600, required_approvals: 2, summary: "Hi {{task.key}}"}
transitions:
  - {from: draft, to: checks, when: succeeded}
  - {from: checks, to: review, when: succeeded}
  - {from: checks, to: draft, when: checks_failed}
  - {from: review, to: shipped, when: approved}
  - {from: review, to: draft, when: changes_requested}
  - {from: review, to: failed, when: rejected}
"""
