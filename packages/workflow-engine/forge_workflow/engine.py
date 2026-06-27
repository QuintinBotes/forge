"""The workflow engine: a Postgres-backed FSM driver.

Implements the frozen :class:`forge_contracts.WorkflowEngine` protocol:

* :meth:`start` opens a run in the workflow's initial state,
* :meth:`transition` resolves a ``(state, event)`` pair against the definition,
  applies retry accounting + terminal-status bookkeeping, persists, and returns
  the new :class:`~forge_contracts.WorkflowState`,
* :meth:`load_definition` parses a DSL document.

Retry/escalation (spec ``retry_policy`` / ``escalation_policy``): a
``checks_failed`` event loops ``verifying -> executing`` while the retry budget
holds (incrementing a counter held in ``run.context``); once exhausted it routes
to ``needs_human_input`` and marks the run ``ESCALATED``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from forge_contracts import (
    RunStatus,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowState,
)
from forge_workflow.default_workflow import default_feature_definition
from forge_workflow.dsl import load_definition as _load_definition
from forge_workflow.fsm import RETRY_BUDGET_REMAINING, TransitionGraph
from forge_workflow.store import InMemoryWorkflowStore, WorkflowStore

#: Key under which the retry counter is held in ``WorkflowRun.context``.
RETRY_COUNT_KEY = "retry_count"

#: States that complete a run successfully.
_SUCCESS_STATES = {WorkflowState.MERGED.value, WorkflowState.CLOSED.value}
#: Terminal/escalation states mapped to a run status.
_STATUS_BY_STATE = {
    WorkflowState.NEEDS_HUMAN_INPUT.value: RunStatus.ESCALATED,
    WorkflowState.FAILED.value: RunStatus.FAILED,
    WorkflowState.CANCELLED.value: RunStatus.CANCELLED,
}


def _now() -> datetime:
    return datetime.now(UTC)


class WorkflowEngineImpl:
    """A concrete :class:`forge_contracts.WorkflowEngine`."""

    def __init__(
        self,
        store: WorkflowStore | None = None,
        definition: WorkflowDefinition | None = None,
        *,
        definitions: list[WorkflowDefinition] | None = None,
    ) -> None:
        self._store: WorkflowStore = store or InMemoryWorkflowStore()
        self._default = definition or default_feature_definition()
        self._graphs: dict[str, TransitionGraph] = {
            self._default.name: TransitionGraph.from_definition(self._default)
        }
        for extra in definitions or []:
            self._graphs[extra.name] = TransitionGraph.from_definition(extra)

    # -- protocol surface ----------------------------------------------------- #

    def start(self, task_id: uuid.UUID) -> WorkflowRun:
        """Open a new run for ``task_id`` in the default workflow's initial state."""
        graph = self._graphs[self._default.name]
        run = WorkflowRun(
            task_id=task_id,
            workflow_name=self._default.name,
            current_state=graph.initial_state,
            status=RunStatus.RUNNING,
            started_at=_now(),
            context={RETRY_COUNT_KEY: 0},
        )
        return self._store.create(run)

    def transition(self, run_id: uuid.UUID, event: str) -> WorkflowState:
        """Apply ``event`` to the run and return its new state."""
        run = self._store.get(run_id)
        graph = self._graph_for(run.workflow_name)
        max_retries = graph.definition.retry_policy.max_retries
        retry_count = int(run.context.get(RETRY_COUNT_KEY, 0))

        chosen = graph.find(
            run.current_state,
            event,
            context=run.context,
            retry_count=retry_count,
            max_retries=max_retries,
        )

        # Taking the "retry budget remaining" edge consumes one retry.
        if chosen.condition == RETRY_BUDGET_REMAINING:
            run.context[RETRY_COUNT_KEY] = retry_count + 1

        run.current_state = chosen.to_state
        run.context["last_event"] = event
        self._apply_status(run, chosen.to_state)
        self._store.update(run)
        return WorkflowState(chosen.to_state)

    def load_definition(self, source: str | object) -> WorkflowDefinition:
        """Parse a workflow DSL document (path or YAML string)."""
        from pathlib import Path

        if not isinstance(source, str | Path):
            raise TypeError(f"source must be a str or Path, got {type(source).__name__}")
        return _load_definition(source)

    # -- extras (beyond the protocol) ----------------------------------------- #

    def get_run(self, run_id: uuid.UUID) -> WorkflowRun:
        """Fetch a persisted run."""
        return self._store.get(run_id)

    def update_context(self, run_id: uuid.UUID, values: dict[str, object]) -> WorkflowRun:
        """Merge external signals (e.g. ``ci_status_green``) into a run's context."""
        run = self._store.get(run_id)
        run.context.update(values)
        return self._store.update(run)

    def register_definition(self, definition: WorkflowDefinition) -> None:
        """Register an additional named workflow definition."""
        self._graphs[definition.name] = TransitionGraph.from_definition(definition)

    def should_escalate(self, confidence: float) -> bool:
        """True when ``confidence`` is below the escalation threshold (spec: 0.72)."""
        return confidence < self._default.escalation_policy.confidence_threshold

    # -- internals ------------------------------------------------------------ #

    def _graph_for(self, workflow_name: str) -> TransitionGraph:
        return self._graphs.get(workflow_name, self._graphs[self._default.name])

    @staticmethod
    def _apply_status(run: WorkflowRun, new_state: str) -> None:
        if new_state in _SUCCESS_STATES:
            run.status = RunStatus.SUCCEEDED
            run.completed_at = _now()
        elif new_state in _STATUS_BY_STATE:
            run.status = _STATUS_BY_STATE[new_state]
            if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
                run.completed_at = _now()
        else:
            run.status = RunStatus.RUNNING


__all__ = ["RETRY_COUNT_KEY", "WorkflowEngineImpl"]
