"""``TemporalWorkflowEngine`` — the V2 durable implementation of ``WorkflowEngine``.

Implements the **same frozen** :class:`scf_contracts.WorkflowEngine` protocol as
F07's in-process FSM (``start`` / ``transition`` / ``load_definition``), plus the
``get_run`` extra the API router uses — so nothing upstream changes. The contract
is sync; Temporal is async, so each protocol method bridges via ``runner``
(``asyncio.run`` by default). Async internals (``astart`` / ``atransition`` / …) are
public so callers already inside an event loop (and the test suite, on the
time-skipping ``WorkflowEnvironment``) can drive the engine without a nested loop.

* ``start`` writes the ``workflow_run`` projection (``engine_backend=temporal``,
  ``temporal_workflow_id=wf-<id>``), rejects a duplicate active run for the task,
  **commits it**, then starts ``forge.FeatureWorkflow`` with ``REJECT_DUPLICATE``
  (the worker's ``persist_transition`` must find the row; spec K §4.11). Given a
  line's revision text (``dsl_yaml``), it starts ``forge.StationWorkflow`` instead,
  on a row in the line's initial state marked ``context.dsl_version = "2"``. A start
  that fails marks the committed row ``failed`` and terminates ``wf-<id>`` in case
  the server started it anyway (the projection then refuses to move that row).
  An active row no workflow has reported to (no ``temporal_run_id``) is started
  again under its own ``wf-<id>`` rather than refused, so a start that died after
  its commit cannot block the task; Temporal refuses it if that workflow exists.
  A line row is reused only for a start of its own revision; a live one reports no
  transition until its first station finishes, and Temporal refuses its ``wf-<id>``.
* ``transition`` is a synchronous Workflow **Update** (cancel is a **Signal**), and
  returns a line run's state as ``str`` (its states are the line's own); an
  invalid event / failed guard surfaces as ``InvalidTransitionError`` /
  ``GuardFailedError`` (→ HTTP 409), identical to the FSM.
* ``get_run`` / ``list_runs`` / ``history`` read the **Postgres projection** — never
  Temporal — so the read path has no Temporal dependency.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scf_contracts import RunStatus, WorkflowDefinition, WorkflowRun, WorkflowState
from scf_workflow.default_workflow import default_feature_definition
from scf_workflow.dsl import load_definition as _load_definition
from scf_workflow.engine import DEFINITION_REVISION_KEY
from scf_workflow.exceptions import (
    DuplicateRunError,
    GuardFailedError,
    InvalidTransitionError,
    PreconditionError,
    WorkflowError,
    WorkflowRunNotFoundError,
)
from scf_workflow.store import InMemoryWorkflowStore, WorkflowStore
from scf_workflow.temporal.config import DEFAULT_TASK_QUEUE
from scf_workflow.temporal.ids import workflow_id
from scf_workflow.temporal.payloads import (
    EVENT_CANCEL,
    EscalationPolicyDTO,
    RetryPolicyDTO,
    StationParams,
    WorkflowEventPayload,
    WorkflowParams,
)
from scf_workflow.temporal.station_workflow import StationWorkflow, load_line
from scf_workflow.temporal.workflows import FeatureWorkflow

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
    """A durable Temporal-backed :class:`scf_contracts.WorkflowEngine`."""

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
        commit: Callable[[], None] | None = None,
        checks_timeout_seconds: int = 1_800,
    ) -> None:
        self._workspace_id = workspace_id
        self._checks_timeout_seconds = checks_timeout_seconds
        self._store: WorkflowStore = store or InMemoryWorkflowStore()
        #: Commits the store's transaction (the API passes ``session.commit``).
        self._commit = commit or (lambda: None)
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
        self,
        task_id: uuid.UUID,
        definition_name: str = "default_feature",
        *,
        trigger: dict[str, object] | None = None,
        definition_revision_id: uuid.UUID | None = None,
        started_by: str = "system",
        dsl_yaml: str | None = None,
        task: dict[str, str] | None = None,
    ) -> WorkflowRun:
        """Start a run; with ``dsl_yaml`` (a line revision's text) a station run.

        ``task`` is a line run's task snapshot (``StationParams.task``).
        """
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        from temporalio.service import RPCError

        line = load_line(dsl_yaml) if dsl_yaml is not None else None
        if line is not None and definition_revision_id is None:
            raise WorkflowError("a line run needs its definition revision")
        active = self._store.find_active_by_task(task_id)
        pinned = str(definition_revision_id) if line is not None else None
        if active is not None and (
            active.context.get("engine_backend") != "temporal"
            or active.context.get("temporal_run_id")
            # Restart only a start of the same kind: a line row with its own revision.
            or (active.context.get("dsl_version") == "2") != (line is not None)
            or (line is not None and active.context.get(DEFINITION_REVISION_KEY) != pinned)
        ):
            raise DuplicateRunError(task_id)
        client = await self._get_client()

        reused = active is not None and active.id is not None
        if active is not None and active.id is not None:
            # No workflow has reported to this run, so its start may have died before
            # start_workflow. Start it under its own id: Temporal refuses a second one.
            run_id, definition_name = active.id, active.workflow_name
            wf_id = workflow_id(run_id)
            started_by = str(active.context.get("started_by", "system"))  # absent: system
        else:
            run_id = uuid.uuid4()
            wf_id = workflow_id(run_id)
            context: dict[str, Any] = {
                "engine_backend": "temporal",
                "temporal_workflow_id": wf_id,
                "retry_count": 0,
                "transitions": [],
            }
            if trigger is not None:
                context["trigger"] = trigger
            if started_by != "system":
                context["started_by"] = started_by
            if definition_revision_id is not None:
                context[DEFINITION_REVISION_KEY] = str(definition_revision_id)
            if line is not None:
                context["dsl_version"] = "2"
            run = WorkflowRun(
                id=run_id,
                task_id=task_id,
                workflow_name=definition_name,
                current_state=line.initial if line else WorkflowState.CREATED.value,
                status=RunStatus.RUNNING,
                started_at=_now(),
                context=context,
            )
            self._store.create(run)  # DuplicateRunError when a concurrent start won the task
        self._commit()  # the workflow's first persist_transition must find this row

        workflow_run: Any = FeatureWorkflow.run
        params: WorkflowParams | StationParams = WorkflowParams(
            workflow_run_id=run_id,
            task_id=task_id,
            workspace_id=self._workspace_id,
            definition_name=definition_name,
            retry_policy=self._retry_policy(definition_name),
            escalation_policy=self._escalation_policy(definition_name),
            started_by=started_by,
            checks_timeout_seconds=self._checks_timeout_seconds,
        )
        if dsl_yaml is not None and definition_revision_id is not None:  # a line run
            workflow_run = StationWorkflow.run
            params = StationParams(
                workflow_run_id=run_id,
                workspace_id=self._workspace_id,
                task_id=task_id,
                definition_revision_id=definition_revision_id,
                dsl_yaml=dsl_yaml,
                task=dict(task or {}),
                trigger=dict(trigger or {}),
            )
        try:
            handle = await client.start_workflow(
                workflow_run,
                params,
                id=wf_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError as exc:
            # This run's workflow exists (another start launched it): the row is live.
            raise DuplicateRunError(task_id) from exc
        except Exception as exc:
            if reused:
                # The row's own start may still be live and its workflow running: an
                # error here proves nothing about it, so leave the row and the workflow.
                raise
            failed = self._store.get(run_id)
            failed.status = RunStatus.FAILED
            failed.completed_at = _now()
            failed.context["start_error"] = f"{type(exc).__name__}: {exc}"
            self._store.update(failed)
            self._commit()
            # The server may have started it although the call failed: stop that orphan.
            with suppress(RPCError):  # includes not-found: nothing was started
                await client.get_workflow_handle(wf_id).terminate(reason="start_failed")
            raise

        current = self._store.find_active_by_task(task_id)
        if current is None or current.id != run_id:
            # A concurrent start failed this run (commit, then terminate) before our
            # workflow existed, so its terminate missed it: stop it here.
            with suppress(RPCError):
                await client.get_workflow_handle(wf_id).terminate(reason="start_failed")
            raise DuplicateRunError(task_id)
        self._handles[run_id] = handle
        return self._store.get(run_id)

    async def atransition(
        self, run_id: uuid.UUID, event: str, *, actor: str = "system", **payload: Any
    ) -> WorkflowState | str:
        run = self._store.get(run_id)
        wf_id = run.context.get("temporal_workflow_id") or workflow_id(run_id)
        client = await self._get_client()
        handle = client.get_workflow_handle(wf_id)
        # A line run's states are its own (spec K §4.9); both workflows share these names.
        state_cast: Any = str if run.context.get("dsl_version") == "2" else WorkflowState

        if event == EVENT_CANCEL:
            reason = payload.get("reason", "cancelled")
            await handle.signal(FeatureWorkflow.cancel_run, args=[reason, actor])
            return state_cast(self._store.get(run_id).current_state)

        try:
            new_state = await handle.execute_update(
                FeatureWorkflow.submit_event,
                WorkflowEventPayload(type=event, actor=actor, payload=dict(payload)),
            )
        except Exception as exc:
            raise self._map_update_error(exc, run.current_state, event) from exc
        return state_cast(new_state)

    async def astation_gate_changed(
        self, run_id: uuid.UUID, approval_request_id: uuid.UUID
    ) -> None:
        """Hint a line run that a human station's gate changed; it re-reads the gate."""
        run = self._store.get(run_id)  # a run outside this workspace is not found
        wf_id = run.context.get("temporal_workflow_id") or workflow_id(run_id)
        client = await self._get_client()
        await client.get_workflow_handle(wf_id).signal(
            StationWorkflow.station_gate_changed, str(approval_request_id)
        )

    async def aget_run(self, run_id: uuid.UUID) -> WorkflowRun:
        return self._store.get(run_id)

    # ------------------------------------------------------------------ #
    # Sync protocol surface (bridges to the async core)                   #
    # ------------------------------------------------------------------ #
    def start(
        self,
        task_id: uuid.UUID,
        definition_name: str = "default_feature",
        *,
        trigger: dict[str, object] | None = None,
        definition_revision_id: uuid.UUID | None = None,
        started_by: str = "system",
        dsl_yaml: str | None = None,
        task: dict[str, str] | None = None,
    ) -> WorkflowRun:
        return self._runner(
            self.astart(
                task_id,
                definition_name,
                trigger=trigger,
                started_by=started_by,
                definition_revision_id=definition_revision_id,
                dsl_yaml=dsl_yaml,
                task=task,
            )
        )

    def transition(
        self, run_id: uuid.UUID, event: str, *, actor: str = "system", **payload: Any
    ) -> WorkflowState | str:
        return self._runner(self.atransition(run_id, event, actor=actor, **payload))

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
