"""F25 — Temporal durable workflow engine (V2).

The V2 spine: a ``TemporalWorkflowEngine`` implementing the **same frozen**
:class:`scf_contracts.WorkflowEngine` protocol as the V1 Postgres FSM, driving
the identical ``default_feature`` lifecycle as a durable Temporal Workflow
(``forge.FeatureWorkflow``) with Updates (human gates), Signals (cancel), Queries
(reads), and idempotent Activities (every effect). LangGraph stays the agent
brain inside the ``run_agent`` Activity; Temporal becomes the durable spine.
"""

from __future__ import annotations

from scf_workflow.temporal.activities import WorkflowActivities
from scf_workflow.temporal.client import build_data_converter, get_temporal_client
from scf_workflow.temporal.config import (
    DEFAULT_NAMESPACE,
    DEFAULT_TASK_QUEUE,
    TemporalSettings,
)
from scf_workflow.temporal.converter import RedactingEncryptionCodec, redact_secrets
from scf_workflow.temporal.determinism import (
    PureGuardContext,
    TransitionDecision,
    TransitionEvaluator,
)
from scf_workflow.temporal.engine import TemporalWorkflowEngine
from scf_workflow.temporal.ids import transition_idempotency_key, workflow_id
from scf_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ChecksResult,
    GuardInputs,
    WorkflowEventPayload,
    WorkflowParams,
    WorkflowResult,
)
from scf_workflow.temporal.worker import build_temporal_worker
from scf_workflow.temporal.workflows import FeatureWorkflow

__all__ = [
    "DEFAULT_NAMESPACE",
    "DEFAULT_TASK_QUEUE",
    "AgentRunResultDTO",
    "ChecksResult",
    "FeatureWorkflow",
    "GuardInputs",
    "PureGuardContext",
    "RedactingEncryptionCodec",
    "TemporalSettings",
    "TemporalWorkflowEngine",
    "TransitionDecision",
    "TransitionEvaluator",
    "WorkflowActivities",
    "WorkflowEventPayload",
    "WorkflowParams",
    "WorkflowResult",
    "build_data_converter",
    "build_temporal_worker",
    "get_temporal_client",
    "redact_secrets",
    "transition_idempotency_key",
    "workflow_id",
]
