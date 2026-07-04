"""Build the Temporal worker that runs ``FeatureWorkflow`` + its Activities (F25).

The worker registers the single ``forge.FeatureWorkflow`` workflow and every
bound Activity from a :class:`WorkflowActivities` instance on the configured task
queue. ``apps/worker/forge_worker/temporal_main.py`` is the process entrypoint.

The workflow sandbox is configured to **pass through** Forge's own packages (and
pydantic/cryptography) so importing the workflow module never re-executes the
heavier ``forge_workflow.temporal`` package body (which pulls in the Rust
``cryptography`` binding) inside the deterministic sandbox. Our workflow code is
deterministic by construction; Temporal still enforces determinism at runtime.
"""

from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from forge_workflow.temporal.activities import WorkflowActivities
from forge_workflow.temporal.config import DEFAULT_TASK_QUEUE
from forge_workflow.temporal.workflows import FeatureWorkflow

#: Modules safe to import directly inside the workflow sandbox (our deterministic
#: code + its serialization deps).
_PASSTHROUGH = (
    "forge_workflow",
    "forge_contracts",
    "pydantic",
    "pydantic_core",
    "cryptography",
    "yaml",
)


def feature_workflow_runner() -> SandboxedWorkflowRunner:
    """The sandbox runner used by both the worker and the replay test."""
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(*_PASSTHROUGH)
    )


def build_temporal_worker(
    client: Client,
    activities: WorkflowActivities,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
) -> Worker:
    """Construct a :class:`temporalio.worker.Worker` for the feature workflow."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[FeatureWorkflow],
        activities=activities.register(),
        workflow_runner=feature_workflow_runner(),
    )


__all__ = ["build_temporal_worker", "feature_workflow_runner"]
