"""Postgres-backed FSM workflow engine and DSL parser for Forge.

Public surface (plan Task 1.8):

* :class:`WorkflowEngineImpl` — the :class:`forge_contracts.WorkflowEngine`
  implementation (``start`` / ``transition`` / ``load_definition``).
* :func:`load_definition` / :func:`parse_definition` — the workflow DSL parser.
* :class:`TransitionGraph` — the validated FSM core.
* :func:`default_feature_definition` — the spec's default feature workflow.
* :class:`InMemoryWorkflowStore` / :class:`SqlAlchemyWorkflowStore` — run stores.
"""

from __future__ import annotations

from forge_workflow.default_workflow import (
    DEFAULT_FEATURE_WORKFLOW_YAML,
    DEFAULT_WORKFLOW_NAME,
    default_feature_definition,
)
from forge_workflow.dsl import load_definition, parse_definition
from forge_workflow.engine import RETRY_COUNT_KEY, WorkflowEngineImpl
from forge_workflow.exceptions import (
    AmbiguousTransitionError,
    InvalidTransitionError,
    WorkflowDefinitionError,
    WorkflowError,
    WorkflowRunNotFoundError,
)
from forge_workflow.fsm import (
    RETRY_BUDGET_EXHAUSTED,
    RETRY_BUDGET_REMAINING,
    TransitionGraph,
    evaluate_guard,
)
from forge_workflow.store import (
    InMemoryWorkflowStore,
    SqlAlchemyWorkflowStore,
    WorkflowStore,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_FEATURE_WORKFLOW_YAML",
    "DEFAULT_WORKFLOW_NAME",
    "RETRY_BUDGET_EXHAUSTED",
    "RETRY_BUDGET_REMAINING",
    "RETRY_COUNT_KEY",
    "AmbiguousTransitionError",
    "InMemoryWorkflowStore",
    "InvalidTransitionError",
    "SqlAlchemyWorkflowStore",
    "TransitionGraph",
    "WorkflowDefinitionError",
    "WorkflowEngineImpl",
    "WorkflowError",
    "WorkflowRunNotFoundError",
    "WorkflowStore",
    "default_feature_definition",
    "evaluate_guard",
    "load_definition",
    "parse_definition",
]
