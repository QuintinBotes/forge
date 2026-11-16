"""Postgres-backed FSM workflow engine and DSL parser for SCF.

Public surface (plan Task 1.8):

* :class:`WorkflowEngineImpl` — the :class:`scf_contracts.WorkflowEngine`
  implementation (``start`` / ``transition`` / ``load_definition``).
* :func:`load_definition` / :func:`parse_definition` — the workflow DSL parser.
* :class:`TransitionGraph` — the validated FSM core.
* :func:`default_feature_definition` — the spec's default feature workflow.
* :class:`InMemoryWorkflowStore` / :class:`SqlAlchemyWorkflowStore` — run stores.
"""

from __future__ import annotations

from scf_workflow.default_workflow import (
    DEFAULT_FEATURE_WORKFLOW_YAML,
    DEFAULT_WORKFLOW_NAME,
    default_feature_definition,
)
from scf_workflow.dsl import load_definition, parse_definition
from scf_workflow.engine import (
    DEFINITION_REVISION_KEY,
    DEFINITION_VERSION_KEY,
    RETRY_COUNT_KEY,
    WorkflowEngineImpl,
)
from scf_workflow.exceptions import (
    AmbiguousTransitionError,
    DuplicateRunError,
    GuardFailedError,
    InvalidTransitionError,
    PreconditionError,
    WorkflowDefinitionError,
    WorkflowError,
    WorkflowRunNotFoundError,
)
from scf_workflow.fsm import (
    RETRY_BUDGET_EXHAUSTED,
    RETRY_BUDGET_REMAINING,
    TransitionGraph,
    evaluate_guard,
)
from scf_workflow.incident import (
    INCIDENT_DEFINITION_NAME,
    INCIDENT_EVENTS,
    INCIDENT_STATES,
    INCIDENT_TERMINAL_STATES,
    IncidentTransitionOutcome,
    allowed_incident_events,
    default_incident_definition,
    drive_incident,
    incident_graph,
)
from scf_workflow.multi_repo import (
    CyclicRepoDependencyError,
    MergePlanBuilder,
    MultipleOrNoPrimaryError,
    MultiRepoMergeGate,
    MultiRepoMerger,
    RepoMergeClient,
    UnknownDependencyRepoError,
)
from scf_workflow.store import (
    InMemoryWorkflowStore,
    SqlAlchemyWorkflowStore,
    WorkflowStore,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_FEATURE_WORKFLOW_YAML",
    "DEFAULT_WORKFLOW_NAME",
    "DEFINITION_REVISION_KEY",
    "DEFINITION_VERSION_KEY",
    "INCIDENT_DEFINITION_NAME",
    "INCIDENT_EVENTS",
    "INCIDENT_STATES",
    "INCIDENT_TERMINAL_STATES",
    "RETRY_BUDGET_EXHAUSTED",
    "RETRY_BUDGET_REMAINING",
    "RETRY_COUNT_KEY",
    "AmbiguousTransitionError",
    "CyclicRepoDependencyError",
    "DuplicateRunError",
    "GuardFailedError",
    "InMemoryWorkflowStore",
    "IncidentTransitionOutcome",
    "InvalidTransitionError",
    "MergePlanBuilder",
    "MultiRepoMergeGate",
    "MultiRepoMerger",
    "MultipleOrNoPrimaryError",
    "PreconditionError",
    "RepoMergeClient",
    "SqlAlchemyWorkflowStore",
    "TransitionGraph",
    "UnknownDependencyRepoError",
    "WorkflowDefinitionError",
    "WorkflowEngineImpl",
    "WorkflowError",
    "WorkflowRunNotFoundError",
    "WorkflowStore",
    "allowed_incident_events",
    "default_feature_definition",
    "default_incident_definition",
    "drive_incident",
    "evaluate_guard",
    "incident_graph",
    "load_definition",
    "parse_definition",
]
