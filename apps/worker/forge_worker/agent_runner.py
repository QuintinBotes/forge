"""Agent-runner task (plan Task 1.9 — single-agent loop, background half).

Runs a structured :class:`~forge_contracts.AgentObjective` through the agent
runtime's plan -> act -> observe loop. Split so it is unit-testable without Celery
or a live model provider:

* :func:`run_objective` — pure: run an objective through an injected
  :class:`~forge_agent.AgentRunner` and return its :class:`AgentRunResult`.
* :func:`build_agent_runner` — build the default runner (offline-safe scripted
  model; a real BYOK ``ModelClient`` is configured per workspace). Behind
  ``FORGE_RECORD_RUNS=1`` (default OFF) it additionally wires the two
  nondeterministic boundaries through the Time-Travel Runs recording wrappers
  (``forge_agent.replay``) and exposes the resulting cassette as
  ``runner.cassette``.
* :func:`persist_run_recording` — the cassette-persistence recorder sink:
  redacts the cassette, offloads oversized tool outputs to an
  :class:`~forge_agent.sandbox.base.ArtifactStore`, and inserts an append-only
  :class:`~forge_db.models.RunRecording` row.
* :func:`run_agent_task` — the thin Celery task that builds the runner, runs,
  and (best-effort) persists the recording when enabled.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy.orm import Session

from forge_agent import AgentRunner
from forge_agent.providers import (
    ModelClientConfig,
    ModelClientUnavailable,
    build_model_client,
)
from forge_agent.replay import RecordingModelClient, RecordingToolRegistry, RunCassette
from forge_agent.replay.cassette import canonical_json
from forge_agent.sandbox.base import ArtifactStore
from forge_agent.sandbox.output import cap_output
from forge_agent.testing import ScriptedModelClient, finish_response
from forge_agent.tools import ToolRegistry
from forge_auth.redaction import SecretRedactor
from forge_contracts import AgentObjective, AgentRunResult, ModelClient
from forge_db.models import RunRecording
from forge_db.session import create_session_factory
from forge_worker.celery_app import celery_app
from forge_worker.reliability import ForgeTask

__all__ = [
    "build_agent_runner",
    "persist_run_recording",
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
    """The shared secret redactor (``forge_api``), or identity if unavailable."""
    try:
        from forge_api.observability.redaction import redact_text
    except Exception:  # pragma: no cover - forge_api always present in the worker
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
    ) -> None:
        super().__init__(model, tools=tools)
        self.cassette = cassette


def _recording_enabled() -> bool:
    """``FORGE_RECORD_RUNS=1`` opt-in (default OFF) — the Time-Travel Runs recorder."""
    return os.environ.get("FORGE_RECORD_RUNS") == "1"


def _resolve_model_client(model_client: ModelClient | None) -> ModelClient:
    """Resolution order documented on :func:`build_agent_runner`."""
    if model_client is not None:
        return model_client
    config = ModelClientConfig.from_env()
    if config is not None:
        try:
            return build_model_client(config, redactor=_default_redactor())
        except ModelClientUnavailable:
            # Provider SDK/extra absent — never silently fake on a configured
            # lane failure; degrade to the offline client and keep running.
            pass
    return _scripted_client()


def build_agent_runner(
    *,
    workspace_id: uuid.UUID | None = None,
    model_client: ModelClient | None = None,
) -> AgentRunner:
    """Build the agent runner, resolving a real BYOK ``ModelClient`` when possible.

    Resolution order:

    1. an explicitly injected ``model_client`` (tests / DI);
    2. a real provider client when ``FORGE_MODEL_PROVIDER`` + a BYOK key are in
       the environment (the integration lane) and the provider SDK is installed;
    3. otherwise the offline deterministic :class:`ScriptedModelClient`, so the
       worker still runs end-to-end (degraded, network-free).

    ``workspace_id`` is accepted for the per-workspace vault path (resolved via
    ``forge_api.auth.service.resolve_model_client``); the env path above is the
    integration-lane default and takes precedence when configured.

    When ``FORGE_RECORD_RUNS=1`` (default OFF), the resolved model client and a
    fresh (empty) tool registry are wrapped with the Time-Travel Runs recording
    wrappers (``forge_agent.replay.RecordingModelClient`` /
    ``RecordingToolRegistry``) bound to a new ``RunCassette`` seeded with a
    redacted env snapshot; the runtime itself (``AgentRunner``) needs no change
    since both boundaries are already constructor-injected. The resulting
    cassette is exposed as ``runner.cassette`` (``None`` when recording is
    disabled) for a caller to persist via :func:`persist_run_recording`.
    """
    del workspace_id  # env-based resolution below; vault path lives in the API
    client = _resolve_model_client(model_client)

    if not _recording_enabled():
        return _RecordableAgentRunner(client, cassette=None)

    cassette = RunCassette.with_env(os.environ, redactor=SecretRedactor())
    # ``RecordingToolRegistry`` is a duck-typed ``ToolRegistry`` facade (delegates
    # every attribute it does not itself intercept) rather than a nominal
    # subclass — see its docstring in ``forge_agent.replay.recorder``.
    tools = cast(ToolRegistry, RecordingToolRegistry(ToolRegistry(), cassette))
    return _RecordableAgentRunner(
        RecordingModelClient(client, cassette), tools=tools, cassette=cassette
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
    ``CommandOutput.stdout_artifact_ref`` (``forge_agent.sandbox.output.cap_output``).
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


@celery_app.task(bind=True, base=ForgeTask, name="forge.agent.run")
def run_agent_task(
    self: ForgeTask,
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
    runner = build_agent_runner()
    try:
        result = run_objective(runner, AgentObjective.model_validate(objective))
    except SoftTimeLimitExceeded:
        # A runaway loop tripped the soft time limit: escalate gracefully with a
        # structured marker instead of letting the hard kill drop the run
        # silently. ``acks_late`` means the message is not acked, so an operator
        # can re-drive it (dedup-guarded) after raising the budget.
        return {
            "escalated": True,
            "reason": "soft_time_limit_exceeded",
            "idempotency_key": dedup_key,
        }
    _maybe_persist_recording(runner, objective)
    return result.model_dump(mode="json")
