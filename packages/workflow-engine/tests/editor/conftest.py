"""Shared fixtures for the F28 editor unit tests."""

from __future__ import annotations

import uuid
from collections.abc import Callable

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
