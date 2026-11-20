"""Agent-runner task (plan Task 1.9 — single-agent loop, background half).

Runs a structured :class:`~scf_contracts.AgentObjective` through the agent
runtime's plan -> act -> observe loop. Split so it is unit-testable without Celery
or a live model provider:

* :func:`run_objective` — pure: run an objective through an injected
  :class:`~scf_agent.AgentRunner` and return its :class:`AgentRunResult`.
* :func:`build_agent_runner` — build the default runner (offline-safe scripted
  model; a real BYOK ``ModelClient`` is configured per workspace). Behind
  ``FORGE_RECORD_RUNS=1`` (default OFF) it additionally wires the two
  nondeterministic boundaries through the Time-Travel Runs recording wrappers
  (``scf_agent.replay``) and exposes the resulting cassette as
  ``runner.cassette``.
* :func:`persist_run_recording` — the cassette-persistence recorder sink:
  redacts the cassette, offloads oversized tool outputs to an
  :class:`~scf_agent.sandbox.base.ArtifactStore`, and inserts an append-only
  :class:`~scf_db.models.RunRecording` row.
* :func:`run_agent_task` — the thin Celery task that builds the runner, runs,
  and (best-effort) persists the recording when enabled.
* :func:`prepare_run`, :func:`execute_run`, :func:`close_out_run` — the run's
  three phases (#238). Prepare and close-out need privileged credentials and stay
  in the worker; execute is the loop a separate run process can own
  (``scf_worker.run_main``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from scf_agent import AgentRunner
from scf_agent.budget import DEFAULT_RUN_TOKEN_BUDGET, BudgetedModelClient
from scf_agent.providers import (
    LocalCliBridgeClient,
    LocalCliModelClient,
    ModelClientConfig,
    ModelClientError,
    ModelClientUnavailable,
    build_model_client,
)
from scf_agent.replay import RecordingModelClient, RecordingToolRegistry, RunCassette
from scf_agent.replay.cassette import canonical_json
from scf_agent.sandbox import _git
from scf_agent.sandbox.base import ArtifactStore
from scf_agent.sandbox.output import cap_output
from scf_agent.testing import ScriptedModelClient, finish_response
from scf_agent.tools import ToolRegistry
from scf_auth.redaction import SecretRedactor
from scf_contracts import (
    AgentObjective,
    AgentRunResult,
    ExecutionMode,
    ModelClient,
    RunEventDraft,
    RunEventType,
    RunStatus,
    Step,
)
from scf_db.models import AgentRun, RunRecording
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_db.session import create_session_factory
from scf_obs.genai import agent_span
from scf_worker.celery_app import celery_app
from scf_worker.dispatch import RepositoryUnavailable, resolve_run_repository
from scf_worker.finalizer import push_and_open_pr, workspace_credentials
from scf_worker.observability.genai import TracedModelClient, TracedToolRegistry
from scf_worker.reliability import ScfTask
from scf_worker.risk_classification import classify_change
from scf_worker.run_events import terminal_drafts
from scf_worker.run_executor import INLINE, configured_executor
from scf_worker.run_record import EXECUTOR_KEY, InProcessRunRecord, RunRecord
from scf_worker.run_workspace import prepared_workspace

__all__ = [
    "PreparedRun",
    "build_agent_runner",
    "close_out_run",
    "execute_run",
    "persist_run_recording",
    "prepare_run",
    "run_agent_task",
    "run_objective",
]

logger = logging.getLogger("forge.agent_runner")

#: Cap (bytes) beyond which a recorded tool-call output is truncated inline and
#: offloaded to the ``ArtifactStore`` (mirrors ``FORGE_SANDBOX_OUTPUT_CAP_BYTES``).
_RECORDING_OUTPUT_CAP_BYTES = 262144


def run_objective(runner: AgentRunner, objective: AgentObjective) -> AgentRunResult:
    """Run ``objective`` through ``runner`` (plan -> act -> observe)."""
    return runner.run(objective)


def _scripted_client() -> ScriptedModelClient:
    """The offline-safe deterministic client used when no BYOK creds are present."""
    return ScriptedModelClient(
        responses=[],
        default=finish_response(
            "Objective acknowledged; no offline model actions were required.",
            confidence=0.9,
        ),
    )


def _default_redactor() -> Callable[[str], str]:
    """The shared secret redactor (``scf_api``), or identity if unavailable."""
    try:
        from scf_api.observability.redaction import redact_text
    except Exception:  # pragma: no cover - scf_api always present in the worker
        return lambda value: value
    return redact_text


class _RecordableAgentRunner(AgentRunner):
    """An ``AgentRunner`` that carries its (possibly ``None``) recording cassette.

    A local subclass — not a change to ``runtime.py`` — purely so
    ``runner.cassette`` is a properly typed attribute the worker can read after
    a run; ``AgentRunner`` itself needs no change since both nondeterministic
    boundaries are already constructor-injected (record/replay wraps the
    injected ``model``/``tools`` from the outside).
    """

    def __init__(
        self,
        model: ModelClient,
        *,
        tools: ToolRegistry | None = None,
        cassette: RunCassette | None = None,
        repo_root: Path | None = None,
        on_step: Callable[[Step], None] | None = None,
    ) -> None:
        super().__init__(model, tools=tools, repo_root=repo_root, on_step=on_step)
        self.cassette = cassette


def _recording_enabled() -> bool:
    """``FORGE_RECORD_RUNS=1`` opt-in (default OFF) — the Time-Travel Runs recorder."""
    return os.environ.get("FORGE_RECORD_RUNS") == "1"


def _run_token_budget() -> int:
    """``FORGE_AGENT_RUN_TOKEN_BUDGET`` — the hard per-run token cap (ADR-0008)."""
    raw = os.environ.get("FORGE_AGENT_RUN_TOKEN_BUDGET", "").strip()
    return int(raw) if raw else DEFAULT_RUN_TOKEN_BUDGET


def configured_model() -> str | None:
    """The model the environment names for agent runs; ``None`` when none is (#173)."""
    config = ModelClientConfig.from_env()
    return config.model if config is not None else None


def assisted_model(client: ModelClient, pinned: str | None) -> str | None:
    """The model ``client`` runs, which each commit's ``Assisted-by:`` names (#214).

    Never a guess: the scripted client runs no model, and a local CLI runs its own
    (``local-cli:<cli>``) whatever the objective pins. A provider's client runs the
    pinned model, else the configured one it was built with.
    """
    if isinstance(client, ScriptedModelClient):
        return None
    if isinstance(client, LocalCliModelClient | LocalCliBridgeClient):
        return client._model_id
    return pinned or configured_model()


def _scripted_agent_allowed() -> bool:
    """``FORGE_ALLOW_SCRIPTED_AGENT`` — the same opt-in the API's agent router honours."""
    return os.environ.get("FORGE_ALLOW_SCRIPTED_AGENT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_model_client(model_client: ModelClient | None) -> ModelClient:
    """Resolution order documented on :func:`build_agent_runner`."""
    if model_client is not None:
        return model_client
    config = ModelClientConfig.from_env()
    if config is not None:
        try:
            return build_model_client(config, redactor=_default_redactor())
        except ModelClientError:
            # Provider SDK/extra absent (``ModelClientUnavailable``), or the
            # provider is configured but unusable — e.g. a local-CLI bridge
            # URL with a blank ``FORGE_CLI_BRIDGE_TOKEN``, which is the
            # operator guide's three-variable setup with one line forgotten
            # and which ``LocalCliBridgeClient.__init__`` rejects with the
            # parent ``ModelClientError``. Catching the parent covers both;
            # catching only the subclass turned that typo into an unhandled
            # exception in the worker instead of the documented degrade.
            # A configured lane that cannot build a client falls through to the
            # same explicit-opt-in check as an unconfigured one below.
            pass
    if not _scripted_agent_allowed():
        # ADR-0007: a run that cannot reach a model must fail, not report canned
        # success. The API's agent router refuses the same way (503).
        raise ModelClientUnavailable(
            "no usable model provider for this agent run. Set FORGE_MODEL_PROVIDER and a "
            "BYOK key, or FORGE_ALLOW_SCRIPTED_AGENT=1 for canned development output."
        )
    # Reached once per run when canned output was explicitly allowed: keep it loud
    # so nobody mistakes scripted output for a real run.
    logger.warning(
        "No live model provider resolved; falling back to the offline "
        "ScriptedModelClient (canned, deterministic output — not a real agent "
        "run). Set FORGE_MODEL_PROVIDER (anthropic|openai) and a BYOK key "
        "(ANTHROPIC_API_KEY / OPENAI_API_KEY, or FORGE_MODEL_API_KEY) to run a "
        "real provider."
    )
    return _scripted_client()


def traced_model_client(client: ModelClient, resolved: ModelClient) -> TracedModelClient:
    """``client`` with an inference span per call, labelled from the ``resolved`` client (#213)."""
    config = None if isinstance(resolved, ScriptedModelClient) else ModelClientConfig.from_env()
    provider = config.provider.value if config is not None else "scf.scripted"
    return TracedModelClient(client, provider=provider, model=assisted_model(resolved, None))


def build_agent_runner(
    *,
    workspace_id: uuid.UUID | None = None,
    model_client: ModelClient | None = None,
    tools: ToolRegistry | None = None,
    repo_root: Path | None = None,
    token_budget: int | None = None,
    on_step: Callable[[Step], None] | None = None,
    genai_spans: bool = False,
) -> AgentRunner:
    """Build the agent runner, resolving a real BYOK ``ModelClient`` when possible.

    Resolution order:

    1. an explicitly injected ``model_client`` (tests / DI);
    2. a real provider client when ``FORGE_MODEL_PROVIDER`` + a BYOK key are in
       the environment (the integration lane) and the provider SDK is installed;
    3. otherwise the offline deterministic :class:`ScriptedModelClient`, so the
       worker still runs end-to-end (degraded, network-free).

    ``workspace_id`` is accepted for the per-workspace vault path (resolved via
    ``scf_api.auth.service.resolve_model_client``); the env path above is the
    integration-lane default and takes precedence when configured.

    When ``FORGE_RECORD_RUNS=1`` (default OFF), the resolved model client and a
    fresh (empty) tool registry are wrapped with the Time-Travel Runs recording
    wrappers (``scf_agent.replay.RecordingModelClient`` /
    ``RecordingToolRegistry``) bound to a new ``RunCassette`` seeded with a
    redacted env snapshot; the runtime itself (``AgentRunner``) needs no change
    since both boundaries are already constructor-injected. The resulting
    cassette is exposed as ``runner.cassette`` (``None`` when recording is
    disabled) for a caller to persist via :func:`persist_run_recording`.

    ``genai_spans`` wraps both boundaries, outermost, in the GenAI span wrappers
    (#213); the caller opens the run's agent span.
    """
    del workspace_id  # env-based resolution below; vault path lives in the API
    resolved = client = _resolve_model_client(model_client)
    if token_budget is not None:
        client = BudgetedModelClient(client, max_tokens=token_budget)
    registry = tools if tools is not None else ToolRegistry()

    cassette = None
    if _recording_enabled():
        cassette = RunCassette.with_env(os.environ, redactor=SecretRedactor())
        client = RecordingModelClient(client, cassette)
        # ``RecordingToolRegistry`` is a duck-typed ``ToolRegistry`` facade (delegates
        # every attribute it does not itself intercept) rather than a nominal
        # subclass — see its docstring in ``scf_agent.replay.recorder``.
        registry = cast(ToolRegistry, RecordingToolRegistry(registry, cassette))
    if genai_spans:
        client = traced_model_client(client, resolved)
        registry = cast(ToolRegistry, TracedToolRegistry(registry))
    return _RecordableAgentRunner(
        client, tools=registry, cassette=cassette, repo_root=repo_root, on_step=on_step
    )


def _shape_cassette_for_persistence(
    cassette: RunCassette,
    *,
    redactor: SecretRedactor,
    artifact_store: ArtifactStore | None,
    cap_bytes: int = _RECORDING_OUTPUT_CAP_BYTES,
) -> dict[str, Any]:
    """Redact + cap-and-offload a cassette snapshot before it is persisted.

    Every string in ``cassette.to_dict()`` is passed through ``redactor`` first
    (secrets never land on the tape). Oversized recorded tool-call outputs are
    then capped at ``cap_bytes``: the inline copy is truncated and, when
    ``artifact_store`` is supplied, the full (already-redacted) text is
    offloaded and referenced by an ``output_artifact_ref`` key — mirroring
    ``CommandOutput.stdout_artifact_ref`` (``scf_agent.sandbox.output.cap_output``).
    """
    shaped = redactor.redact_value(cassette.to_dict())
    for call in shaped.get("tool_calls", []):
        result = call.get("result")
        if not isinstance(result, dict):
            continue
        output = result.get("output")
        if not isinstance(output, str):
            continue
        capped, ref = cap_output(
            output,
            cap_bytes=cap_bytes,
            store=artifact_store,
            key=f"run-recording/{call.get('index')}-{uuid.uuid4().hex}.output.txt",
        )
        result["output"] = capped
        if ref is not None:
            result["output_artifact_ref"] = ref
    return shaped


def persist_run_recording(
    session: Session,
    cassette: RunCassette,
    *,
    workspace_id: uuid.UUID,
    agent_run_id: uuid.UUID | None = None,
    workflow_run_id: uuid.UUID | None = None,
    redactor: SecretRedactor | None = None,
    artifact_store: ArtifactStore | None = None,
) -> RunRecording:
    """Persist a recorded cassette as an append-only ``RunRecording`` row.

    The cassette-persistence recorder sink for Time-Travel Runs: the cassette is
    redacted and its oversized tool outputs offloaded (see
    :func:`_shape_cassette_for_persistence`), the shaped result is content-hashed
    (sha256 of its canonical JSON), and the row is inserted + flushed (never
    updated afterwards — ``RunRecording`` is append-only, enforced on Postgres by
    ``attach_immutability_trigger``). Committing is the caller's responsibility.
    """
    shaped = _shape_cassette_for_persistence(
        cassette, redactor=redactor or SecretRedactor(), artifact_store=artifact_store
    )
    content_hash = _sha256(canonical_json(shaped))
    model = cassette.llm_calls[-1].model if cassette.llm_calls else None
    row = RunRecording(
        workspace_id=workspace_id,
        agent_run_id=agent_run_id,
        workflow_run_id=workflow_run_id,
        cassette=shaped,
        model=model,
        content_hash=content_hash,
    )
    session.add(row)
    session.flush()
    return row


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _opt_uuid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


def _maybe_persist_recording(runner: AgentRunner, objective: dict[str, Any]) -> None:
    """Best-effort: persist ``runner.cassette`` when recording produced one.

    Requires ``objective["context"]["workspace_id"]`` to scope the row (there is
    no durable ``AgentRun``/``WorkflowRun`` linkage wired into this Celery task
    yet — PARKED pending that integration); missing identity or a DB error is
    logged and swallowed rather than raised, since recording is an
    observability side-channel and must never fail the run itself (mirrors the
    fail-open ``audit.record`` sink in ``tasks/audit.py``).
    """
    cassette = getattr(runner, "cassette", None)
    if cassette is None:
        return
    context = objective.get("context") or {}
    raw_workspace_id = context.get("workspace_id")
    if not raw_workspace_id:
        logger.debug("FORGE_RECORD_RUNS=1 but no context.workspace_id — skipping persist")
        return
    try:
        workspace_id = uuid.UUID(str(raw_workspace_id))
        session_factory = create_session_factory()
        with session_factory() as session:
            persist_run_recording(
                session,
                cassette,
                workspace_id=workspace_id,
                agent_run_id=_opt_uuid(context.get("agent_run_id")),
                workflow_run_id=_opt_uuid(context.get("workflow_run_id")),
            )
            session.commit()
    except Exception:
        logger.exception("failed to persist run recording (non-fatal)")


@celery_app.task(bind=True, base=ScfTask, name="forge.agent.run")
def run_agent_task(
    self: ScfTask,
    objective: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Celery entrypoint: run an agent objective and return its result.

    ``idempotency_key`` (defaulting to the objective's ``task_id``) guards against
    a re-delivered / retried enqueue starting a second run: the duplicate returns
    a ``{"deduplicated": True}`` marker instead of re-invoking the model loop.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    dedup_key = idempotency_key or objective.get("task_id")
    if self.is_duplicate(dedup_key):
        return {"deduplicated": True, "idempotency_key": dedup_key}
    refused = _refuse_ungoverned_run(AgentObjective.model_validate(objective))
    if refused is not None:
        return refused.model_dump(mode="json")
    executor = configured_executor()
    if executor != INLINE:
        # #239: the run's own unit runs the loop; the worker never runs it instead.
        from scf_worker.launched_runs import launch_run

        return launch_run(AgentObjective.model_validate(objective), executor)
    if objective.get("execution_mode") == ExecutionMode.SUPERVISED_MULTI_AGENT.value:
        # Task 17 (F27): a supervised-mode objective is driven by the
        # deterministic multi-agent Supervisor, never a single AgentRunner.
        # FORGE_RECORD_RUNS is a documented no-op on this branch (see the
        # scf_worker.multi_agent module docstring).
        from scf_worker.multi_agent import run_supervised_objective

        try:
            result = run_supervised_objective(objective)
        except SoftTimeLimitExceeded:
            return {
                "escalated": True,
                "reason": "soft_time_limit_exceeded",
                "idempotency_key": dedup_key,
            }
        return result.model_dump(mode="json")
    try:
        result, _ = run_agent_objective(AgentObjective.model_validate(objective))
    except SoftTimeLimitExceeded:
        # The run is already recorded ESCALATED. ``acks_late`` means the message is
        # not acked, so an operator can re-drive it (dedup-guarded) after raising
        # the budget.
        return {
            "escalated": True,
            "reason": "soft_time_limit_exceeded",
            "idempotency_key": dedup_key,
        }
    return result.model_dump(mode="json")


def _refuse_ungoverned_run(objective: AgentObjective) -> AgentRunResult | None:
    """Refuse, without running it, a repository-changing run outside development (#277).

    Admission refuses it first (``create_and_enqueue_run``); this holds for anything
    already enqueued. The refusal is recorded on the run's row when it has one.
    """
    from scf_api.services.agent_dispatch import UngovernedRunRefused, refuse_ungoverned

    try:
        refuse_ungoverned(objective)
    except UngovernedRunRefused as exc:
        context = objective.context
        run_id = _opt_uuid(context.get("agent_run_id"))
        refused = AgentRunResult(
            run_id=run_id, task_id=objective.task_id, status=RunStatus.FAILED, error=exc.reason
        )
        _record_terminal(
            objective,
            run_id or uuid.uuid4(),
            _opt_uuid(context.get("workspace_id")) or uuid.UUID(int=0),
            refused,
            events=None,
            persisted_row=run_id is not None,
        )
        return refused
    return None


def run_agent_objective(
    parsed: AgentObjective, *, deliver: bool = True
) -> tuple[AgentRunResult, AgentObjective]:
    """Run one agent objective: the body the Celery task and ``forge.run_agent`` share.

    The three phases in order (#238): :func:`prepare_run`, :func:`execute_run` and
    :func:`close_out_run`. ``deliver`` pushes the branch and opens the pull request;
    a workflow run opens its own through ``forge.open_pr_with_spec_traceability``.
    Returns the result and the objective with its repository resolved. A failure,
    or Celery's soft time limit, is recorded on the run and then re-raised.
    """
    run, refused = prepare_run(parsed)
    result = refused
    if result is None:
        with _closed_out_on_failure(run):
            result = execute_run(run)
    close_out_run(run, result, deliver=deliver)
    return result, run.objective


@dataclass
class PreparedRun:
    """A run the worker has prepared: its identity, repository and event stream."""

    objective: AgentObjective
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    persisted_row: bool
    events: RunRecord | None


def prepare_run(parsed: AgentObjective) -> tuple[PreparedRun, AgentRunResult | None]:
    """Before the loop, in the worker: the started stamp and the run's repository.

    A persisted run (``context["agent_run_id"]``) is marked started and streams its
    events. Resolving the repository fetches the mirror with source-control
    application credentials, so it stays here. Returns the refusal to close out
    when the repository is unavailable.
    """
    context = parsed.context
    persisted_row = context.get("agent_run_id") is not None
    run_id = _opt_uuid(context.get("agent_run_id")) or uuid.uuid4()
    workspace_id = _opt_uuid(context.get("workspace_id")) or uuid.UUID(int=0)
    if persisted_row:
        _mark_started(run_id, workspace_id)
    events = _event_sink(run_id, workspace_id) if persisted_row else None
    run = PreparedRun(parsed, run_id, workspace_id, persisted_row, events)
    with _closed_out_on_failure(run):
        try:
            run.objective = resolve_run_repository(parsed)
        except RepositoryUnavailable as exc:
            return run, AgentRunResult(
                run_id=run_id, task_id=parsed.task_id, status=RunStatus.FAILED, error=str(exc)
            )
    return run, None


def execute_run(run: PreparedRun, *, persist_recording: bool = True) -> AgentRunResult:
    """The loop: workspace, model loop, step events and (best effort) recording.

    Needs model access, the run's event stream and the sandbox, never source-control
    application credentials. The agent works in a per-run worktree of the resolved
    repository; the run branch outlives it for close-out to deliver.
    """
    parsed, events = run.objective, run.events
    # Resolved first, so each commit names the model this client runs (#214).
    client = _resolve_model_client(None)
    with prepared_workspace(
        parsed,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        model=assisted_model(client, parsed.model),
    ) as workspace:
        if events is not None:
            events.emit(
                RunEventDraft(
                    type=RunEventType.THREAD_STARTED,
                    summary=parsed.objective[:120],
                    payload={
                        "repo": parsed.context.get("repo_full_name"),
                        "branch": workspace.branch if workspace else None,
                    },
                )
            )
        runner = build_agent_runner(
            model_client=client,
            tools=workspace.tools if workspace else None,
            repo_root=workspace.path if workspace else None,
            token_budget=_run_token_budget(),
            on_step=events.on_step if events is not None else None,
            genai_spans=True,
        )
        with agent_span(run_id=run.run_id, workspace_id=run.workspace_id):
            result = run_objective(runner, parsed)
        if workspace is not None:
            # The branch outlives the worktree; close-out delivers it.
            result.artifacts["branch"] = workspace.branch
            head = _git(workspace.path, "rev-parse", "HEAD").stdout.strip()
            result.artifacts["head_commit"] = head
            result.artifacts["base_commit"] = workspace.base_commit
    if persist_recording:
        _maybe_persist_recording(runner, parsed.model_dump(mode="json"))
    return result


def close_out_run(
    run: PreparedRun, result: AgentRunResult, *, deliver: bool = True, reuse_existing: bool = False
) -> None:
    """After the loop, in the worker: delivery, risk, terminal events, outcome, write-back.

    ``reuse_existing`` delivers to a pull request already open for the branch, for a
    close-out that may be a retry.
    """
    branch = result.artifacts.get("branch")
    if deliver and branch and result.status == RunStatus.SUCCEEDED:
        with _closed_out_on_failure(run):
            _finalize(
                run.objective,
                result,
                branch=str(branch),
                run_id=run.run_id,
                reuse_existing=reuse_existing,
            )
    if run.persisted_row:  # best effort: never alters the run (#193)
        classify_change(
            run.objective,
            result,
            run_id=run.run_id,
            workspace_id=run.workspace_id,
            sessions=create_session_factory,
            redact=_default_redactor(),
        )
    _record_terminal(
        run.objective,
        run.run_id,
        run.workspace_id,
        result,
        events=run.events,
        persisted_row=run.persisted_row,
    )


def failure_result(
    run_id: uuid.UUID, task_id: uuid.UUID | None, exc: BaseException
) -> AgentRunResult:
    """The result a failure leaves: Celery's soft time limit escalates, anything else fails."""
    from celery.exceptions import SoftTimeLimitExceeded

    if isinstance(exc, SoftTimeLimitExceeded):
        return AgentRunResult(
            run_id=run_id,
            task_id=task_id,
            status=RunStatus.ESCALATED,
            error="soft_time_limit_exceeded",
        )
    return AgentRunResult(
        run_id=run_id, task_id=task_id, status=RunStatus.FAILED, error=type(exc).__name__
    )


@contextmanager
def _closed_out_on_failure(run: PreparedRun) -> Iterator[None]:
    """Close the run out on a failure, then re-raise so the caller's own semantics apply.

    A runaway loop that trips the soft time limit is recorded ESCALATED, so the run
    does not stay PENDING and block its issue or cap slot; the caller turns it into
    its marker.
    """
    try:
        yield
    except Exception as exc:
        _record_terminal(
            run.objective,
            run.run_id,
            run.workspace_id,
            failure_result(run.run_id, run.objective.task_id, exc),
            events=run.events,
            persisted_row=run.persisted_row,
        )
        raise


def _record_terminal(
    objective: AgentObjective,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    result: AgentRunResult,
    *,
    events: RunRecord | None,
    persisted_row: bool,
) -> None:
    """Close a run out: terminal events, outcome, then tracker write-back.

    Terminal events first: a stream that sees the run's final status may
    stop, so its last events must already be stored. Best-effort on the
    failure paths: a recording error must never mask the original exception
    or marker (the sinks themselves already swallow their own errors).

    A run with no workflow run passed no gate, and its record says so (#277).
    """
    if objective.context.get("workflow_run_id") is None:
        result.artifacts["governance"] = "none"
    _emit_terminal(events, result)
    if persisted_row:
        _record_run_outcome(run_id, workspace_id, result)
        _maybe_tracker_writeback(objective, run_id, workspace_id, result)


def _mark_started(run_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Stamp ``started_at`` when a worker picks the run up (best effort)."""
    try:
        with create_session_factory()() as session:
            row = session.get(AgentRun, run_id)
            if row is not None and row.workspace_id == workspace_id and row.started_at is None:
                row.started_at = datetime.now(UTC)
                session.commit()
    except Exception:
        logger.exception("could not mark agent run %s started", run_id)


def _event_sink(run_id: uuid.UUID, workspace_id: uuid.UUID) -> RunRecord:
    return InProcessRunRecord(
        run_id,
        workspace_id,
        session_factory=create_session_factory(),
        redact=_default_redactor(),
    )


def _emit_terminal(events: RunRecord | None, result: AgentRunResult) -> None:
    if events is None:
        return
    for draft in terminal_drafts(result):
        events.emit(draft)


def _record_run_outcome(
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    result: AgentRunResult,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> None:
    """Write a run's terminal state onto the ``agent_run`` row the API created.

    Scoped to the row's own workspace, so a forged id from another tenant is a no-op.
    A failure is logged, not raised: the result is still returned to the broker.
    """
    try:
        factory = session_factory or create_session_factory()
        with factory() as session:
            row = session.get(AgentRun, run_id, with_for_update=True)
            if row is None or row.workspace_id != workspace_id:
                return
            if row.context_refs:
                # The bundle's refs travel with the recorded outcome (spec J §2.5).
                result.artifacts["context_refs"] = list(row.context_refs)
            row.status = DbRunStatus(result.status.value)
            row.steps = [step.model_dump(mode="json") for step in result.steps]
            # Preserve the write-back and executor claims: replacing output wholesale
            # would erase their at-most-once guards on a redelivery.
            previous = row.output or {}
            row.output = result.model_dump(mode="json") | {
                key: previous[key]
                for key in ("tracker_writeback", EXECUTOR_KEY)
                if previous.get(key)
            }
            row.confidence = result.confidence
            row.completed_at = datetime.now(UTC)
            session.commit()
    except Exception:
        logger.exception("failed to record the outcome of agent run %s", run_id)


def _maybe_tracker_writeback(
    objective: AgentObjective,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    result: AgentRunResult,
    *,
    session_factory: Any = None,
    connections: Any = None,
) -> None:
    """Spec M M2: one run-state comment to the originating tracker issue.

    Runs after :func:`_record_run_outcome` for every terminal state. Only a
    forge-set ``context["tracker"]`` block (stripped from callers at run
    creation) triggers it. Best-effort by construction —
    :func:`scf_worker.tasks.pm_dispatch.post_run_writeback` never raises,
    and this wrapper swallows even seam failures so a tracker outage can
    never change a run's recorded outcome.
    """
    tracker = objective.context.get("tracker")
    if not isinstance(tracker, dict) or not tracker.get("connection_id"):
        return
    try:
        from scf_worker.tasks.pm_dispatch import post_run_writeback

        factory = session_factory or create_session_factory()
        service = connections
        if service is None:  # pragma: no cover - prod seam
            from scf_worker.tasks.pm_dispatch import _connection_service

            service = _connection_service(factory)
        post_run_writeback(
            factory,
            run_id=run_id,
            workspace_id=workspace_id,
            result=result,
            tracker=tracker,
            connections=service,
        )
    except Exception:
        logger.exception("tracker write-back seam failed for run %s (non-fatal)", run_id)


def _commits_ahead(repo_path: Path, base: str, branch: str) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..{branch}"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip() or 0)


def _finalize(
    objective: AgentObjective,
    result: AgentRunResult,
    *,
    branch: str,
    run_id: uuid.UUID,
    reuse_existing: bool = False,
) -> None:
    """Open the run's PR when the objective names a GitHub repository (spec G §4).

    Records the outcome in ``result.artifacts["pull_request"]``: ``opened`` with
    the PR, ``no_changes``, ``not_configured`` (the workspace has no GitHub App
    credentials, #186), or a failure that fails the run — delivery that did not
    happen is not success.
    """
    from scf_api.services.agent_dispatch import ungoverned_runs_allowed
    from scf_api.services.workflow_engine import resolve_backend

    repo_full_name = objective.context.get("repo_full_name")
    if not repo_full_name:
        return
    if resolve_backend() == "temporal" or not ungoverned_runs_allowed():
        # This run has no workflow run. With the durable engine selected, only a
        # workflow run delivers (FR-007); without it, only development may (#277).
        result.artifacts["pull_request"] = {"status": "ungoverned_refused"}
        return
    repo_path = Path(str(objective.context["repo_path"]))
    target = objective.primary_repo_target
    base = target.base_branch if target else "main"
    if _commits_ahead(repo_path, base, branch) == 0:
        result.artifacts["pull_request"] = {"status": "no_changes"}
        return
    workspace_id = uuid.UUID(str(objective.context["workspace_id"]))
    credentials = workspace_credentials(workspace_id, str(repo_full_name))
    if credentials is None:
        result.artifacts["pull_request"] = {"status": "not_configured"}
        return
    title = f"{objective.key + ': ' if objective.key else ''}{objective.objective}"[:120]
    body = (
        f"Opened by SCF agent run `{run_id}`.\n\n{result.summary or result.output or ''}\n\n"
        "**Ungoverned run:** this change was made with no workflow run, so it passed "
        "no spec or plan gate and carries no attestation. SCF allows that only in a "
        "development environment.\n\n"
        "Merging requires human approval through the SCF `pr` gate."
    )
    try:
        pr = push_and_open_pr(
            repo_path=repo_path,
            branch=branch,
            repo_full_name=str(repo_full_name),
            base_branch=base,
            title=title,
            body=body,
            credentials=credentials,
            workspace_id=workspace_id,
            run_id=run_id,
            reuse_existing=reuse_existing,
        )
    except Exception as exc:  # push or API failure: the change was not delivered
        # The shared redactor knows no PEM block; the canonical one does (#186).
        message = SecretRedactor().redact(_default_redactor()(f"{type(exc).__name__}: {exc}"))
        result.status = RunStatus.FAILED
        result.error = f"pull request could not be opened: {message}"
        result.artifacts["pull_request"] = {"status": "failed", "error": message}
        return
    result.artifacts["pull_request"] = {"status": "opened", "number": pr.number, "url": pr.url}
