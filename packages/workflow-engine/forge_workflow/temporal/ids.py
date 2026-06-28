"""Stable id helpers for Temporal workflows + idempotent activities (F25).

Deterministic id construction is critical: the workflow id binds 1:1 to a
``workflow_run`` row (and is protected by the partial-unique index +
``WorkflowIdReusePolicy.REJECT_DUPLICATE``), and activity idempotency keys make
Temporal's at-least-once Activity delivery safe (a redelivered ``persist_transition``
or ``open_pr`` never double-writes).
"""

from __future__ import annotations

import uuid

#: Prefix for every Forge feature workflow id.
WORKFLOW_ID_PREFIX = "wf-"


def workflow_id(workflow_run_id: uuid.UUID | str) -> str:
    """Return the Temporal workflow id for a ``workflow_run`` row id."""
    return f"{WORKFLOW_ID_PREFIX}{workflow_run_id}"


def workflow_run_id_from(workflow_id_value: str) -> uuid.UUID:
    """Inverse of :func:`workflow_id`; raises ``ValueError`` on a bad id."""
    if not workflow_id_value.startswith(WORKFLOW_ID_PREFIX):
        raise ValueError(f"not a forge workflow id: {workflow_id_value!r}")
    return uuid.UUID(workflow_id_value[len(WORKFLOW_ID_PREFIX) :])


def transition_idempotency_key(
    workflow_run_id: uuid.UUID | str, sequence: int
) -> str:
    """Idempotency key for a persisted transition (unique per run + sequence)."""
    return f"{workflow_run_id}:transition:{sequence}"


def activity_idempotency_key(
    workflow_run_id: uuid.UUID | str, effect: str, attempt: int
) -> str:
    """Idempotency key for a side-effecting activity (e.g. ``open_pr``)."""
    return f"{workflow_run_id}:{effect}:{attempt}"


__all__ = [
    "WORKFLOW_ID_PREFIX",
    "activity_idempotency_key",
    "transition_idempotency_key",
    "workflow_id",
    "workflow_run_id_from",
]
