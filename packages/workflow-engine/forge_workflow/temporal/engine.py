"""``TemporalWorkflowEngine`` — the V2 durable implementation of ``WorkflowEngine``.

Implements the **same frozen** :class:`forge_contracts.WorkflowEngine` protocol as
F07's in-process FSM (``start`` / ``transition`` / ``load_definition``), plus the
``get_run`` extra the API router uses — so nothing upstream changes. The contract
is sync; Temporal is async, so each protocol method bridges via ``runner``
(``asyncio.run`` by default). Async internals (``astart`` / ``atransition`` / …) are
public so callers already inside an event loop (and the test suite, on the
time-skipping ``WorkflowEnvironment``) can drive the engine without a nested loop.

* ``start`` writes the ``workflow_run`` projection (``engine_backend=temporal``,
  ``temporal_workflow_id=wf-<id>``), rejects a duplicate active run for the task,
  then starts ``forge.FeatureWorkflow`` with ``REJECT_DUPLICATE``.
* ``transition`` is a synchronous Workflow **Update** (cancel is a **Signal**); an
  invalid event / failed guard surfaces as ``InvalidTransitionError`` /
  ``GuardFailedError`` (→ HTTP 409), identical to the FSM.
* ``get_run`` / ``list_runs`` / ``history`` read the **Postgres projection** — never
  Temporal — so the read path has no Temporal dependency.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge_contracts import RunStatus, WorkflowDefinition, WorkflowRun, WorkflowState
from forge_workflow.default_workflow import default_feature_definition
from forge_workflow.dsl import load_definition as _load_definition
from forge_workflow.exceptions import (
    DuplicateRunError,
    GuardFailedError,
    InvalidTransitionError,
    PreconditionError,
    WorkflowError,
    WorkflowRunNotFoundError,
)
from forge_workflow.store import InMemoryWorkflowStore, WorkflowStore
from forge_workflow.temporal.config import DEFAULT_TASK_QUEUE
from forge_workflow.temporal.ids import workflow_id
from forge_workflow.temporal.payloads import (
    EVENT_CANCEL,
    EscalationPolicyDTO,
    RetryPolicyDTO,
    WorkflowEventPayload,
    WorkflowParams,
)
from forge_workflow.temporal.workflows import FeatureWorkflow

if TYPE_CHECKING:
    from temporalio.client import Client

ClientFactory = Callable[[], Awaitable["Client"]]

_ERROR_TYPES: dict[str, type[WorkflowError]] = {
    "InvalidTransitionError": InvalidTransitionError,
    "GuardFailedError": GuardFailedError,
    "PreconditionError": PreconditionError,
}


def _now() -> datetime:
    return datetime.now(UTC)


class TemporalWorkflowEngine:
    """A durable Temporal-backed :class:`forge_contracts.WorkflowEngine`."""

    def __init__(
        self,
        *,
        workspace_id: uuid.UUID,
        store: WorkflowStore | None = None,
        client: Client | None = None,
        client_factory: ClientFactory | None = None,
        task_queue: str = DEFAULT_TASK_QUEUE,
        definitions: list[WorkflowDefinition] | None = None,
        runner: Callable[[Awaitable[Any]], Any] | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._store: WorkflowStore = store or InMemoryWorkflowStore()
        self._client = client
        self._client_factory = client_factory
        self._task_queue = task_queue
        self._default = default_feature_definition()
        self._definitions = {self._default.name: self._default}
        for extra in definitions or []:
            self._definitions[extra.name] = extra
        self._runner = runner or asyncio.run
        # Handles returned by ``start_workflow`` (used by callers already in the
        # event loop, e.g. tests on the time-skipping env, to await results with
        # auto time-skip — re-fetched handles do not auto-skip).
        self._handles: dict[uuid.UUID, object] = {}

    @property
    def store(self) -> WorkflowStore:
        return self._store

    def workflow_handle(self, run_id: uuid.UUID) -> object | None:
        """The ``start_workflow`` handle for ``run_id`` if this engine started it."""
        return self._handles.get(run_id)

    # ------------------------------------------------------------------ #
    # Client resolution                                                   #
    # ------------------------------------------------------------------ #
    async def _get_client(self) -> Client:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            return await self._client_factory()
        raise WorkflowError("TemporalWorkflowEngine has no client or client_factory")

    # ------------------------------------------------------------------ #
    # Async core                                                          #
    # ------------------------------------------------------------------ #
    async def astart(
        self, task_id: uuid.UUID, definition_name: str = "default_feature"
    ) -> WorkflowRun:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        if self._store.find_active_by_task(task_id) is not None:
            raise DuplicateRunError(task_id)

        run_id = uuid.uuid4()
        wf_id = workflow_id(run_id)
        run = WorkflowRun(
            id=run_id,
            task_id=task_id,
            workflow_name=definition_name,
            current_state=WorkflowState.CREATED.value,
            status=RunStatus.RUNNING,
            started_at=_now(),
            context={
                "engine_backend": "temporal",
                "temporal_workflow_id": wf_id,
                "retry_count": 0,
                "transitions": [],
            },
        )
        self._store.create(run)

        params = WorkflowParams(
            workflow_run_id=run_id,
            task_id=task_id,
            workspace_id=self._workspace_id,
            definition_name=definition_name,
            retry_policy=self._retry_policy(definition_name),
            escalation_policy=self._escalation_policy(definition_name),
        )
        client = await self._get_client()
        try:
            handle = await client.start_workflow(
                FeatureWorkflow.run,
                params,
                id=wf_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError as exc:
            raise DuplicateRunError(task_id) from exc

        self._handles[run_id] = handle
        stored = self._store.get(run_id)
        stored.context["temporal_run_id"] = handle.result_run_id
        self._store.update(stored)
        return self._store.get(run_id)

    async def atransition(
        self, run_id: uuid.UUID, event: str, *, actor: str = "system", **payload: Any
    ) -> WorkflowState:
        run = self._store.get(run_id)
        wf_id = run.context.get("temporal_workflow_id") or workflow_id(run_id)
        client = await self._get_client()
        handle = client.get_workflow_handle(wf_id)

        if event == EVENT_CANCEL:
            await handle.signal(FeatureWorkflow.cancel_run, payload.get("reason", "cancelled"))
            return WorkflowState(self._store.get(run_id).current_state)

        try:
            new_state = await handle.execute_update(
                FeatureWorkflow.submit_event,
                WorkflowEventPayload(type=event, actor=actor, payload=dict(payload)),
            )
        except Exception as exc:
            raise self._map_update_error(exc, run.current_state, event) from exc
        return WorkflowState(new_state)

    async def aget_run(self, run_id: uuid.UUID) -> WorkflowRun:
        return self._store.get(run_id)

    # ------------------------------------------------------------------ #
    # Sync protocol surface (bridges to the async core)                   #
    # ------------------------------------------------------------------ #
    def start(self, task_id: uuid.UUID, definition_name: str = "default_feature") -> WorkflowRun:
        return self._runner(self.astart(task_id, definition_name))

    def transition(self, run_id: uuid.UUID, event: str) -> WorkflowState:
        return self._runner(self.atransition(run_id, event))

    def load_definition(self, source: str | Path) -> WorkflowDefinition:
        if not isinstance(source, str | Path):
            raise TypeError(f"source must be a str or Path, got {type(source).__name__}")
        return _load_definition(source)

    # -- read path: Postgres projection only, never Temporal ------------- #
    def get_run(self, run_id: uuid.UUID) -> WorkflowRun:
        return self._store.get(run_id)

    def list_runs(self, *, task_id: uuid.UUID | None = None) -> list[WorkflowRun]:
        if task_id is None:
            raise WorkflowError("list_runs requires a task_id for the projection store")
        return self._store.list_by_task(task_id)

    def history(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        run = self._store.get(run_id)
        return list(run.context.get("transitions", []))

    def definition(self, name: str = "default_feature") -> WorkflowDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise WorkflowError(f"unknown workflow definition {name!r}") from exc

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #
    def _retry_policy(self, definition_name: str) -> RetryPolicyDTO:
        d = self._definitions.get(definition_name, self._default).retry_policy
        return RetryPolicyDTO(
            max_retries=d.max_retries,
            backoff=d.backoff,
            initial_delay_seconds=d.initial_delay_seconds,
        )

    def _escalation_policy(self, definition_name: str) -> EscalationPolicyDTO:
        e = self._definitions.get(definition_name, self._default).escalation_policy
        return EscalationPolicyDTO(
            confidence_threshold=e.confidence_threshold,
            on_low_confidence=e.on_low_confidence,
            on_policy_conflict=e.on_policy_conflict,
        )

    @staticmethod
    def _map_update_error(exc: Exception, state: str, event: str) -> WorkflowError:
        if isinstance(exc, WorkflowError):
            return exc
        if isinstance(exc, WorkflowRunNotFoundError):
            return exc
        error_type = _extract_error_type(exc)
        cls = _ERROR_TYPES.get(error_type)
        if cls is InvalidTransitionError:
            return InvalidTransitionError(state, event)
        if cls is GuardFailedError:
            return GuardFailedError(state, event)
        if cls is PreconditionError:
            return PreconditionError(state, event, [])
        return InvalidTransitionError(state, event)


def _extract_error_type(exc: Exception) -> str:
    """Pull the ApplicationError ``type`` out of a Temporal update failure chain."""
    from temporalio.exceptions import ApplicationError

    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ApplicationError) and cur.type:
            return cur.type
        for nxt in (getattr(cur, "cause", None), cur.__cause__):
            if isinstance(nxt, BaseException):
                stack.append(nxt)
    return type(exc).__name__


__all__ = ["TemporalWorkflowEngine"]
