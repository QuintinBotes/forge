"""Supervised multi-agent dispatch (Task 17 — worker-side wiring of F27).

``forge.agent.run`` branches here when an :class:`~forge_contracts.AgentObjective`
selects ``execution_mode=supervised_multi_agent``: the run is driven by the
deterministic F27 :class:`~forge_coordinator.Supervisor` instead of a single
:class:`~forge_agent.AgentRunner`, returning the same ``AgentRunResult`` contract
(the Supervisor implements the foundation ``AgentRuntime`` protocol, so the task's
observable contract — a JSON-serialisable result dict, dedup marker, soft-time-limit
escalation — is unchanged).

Glue provided here, all injectable for tests:

* model clients — each subagent runtime is a real ``AgentRunner`` on the client
  resolved ONCE per run by :func:`forge_worker.agent_runner._resolve_model_client`
  (BYOK env lane, or the loud Task-1 scripted fallback);
* persistence — ``sub_agent_run`` rows land via the coordinator's own
  :class:`~forge_coordinator.SqlAlchemySubAgentRunSink` when the database and the
  objective's identity (``context.workspace_id`` + ``context.parent_agent_run_id``)
  are available. The supervisor's parent ``agent_run`` row (``is_supervisor=True``)
  is ensured before dispatch (it is the FK target of every ``sub_agent_run`` row)
  and finalized best-effort after the run. Without a reachable DB or identity the
  adapter degrades to the in-memory sink with a WARNING instead of failing the run
  (mirrors the fail-open recording sink in ``agent_runner``).

Recording limitation (Time-Travel Runs): ``FORGE_RECORD_RUNS=1`` is a documented
no-op on this path. The single-agent recorder wraps exactly one runner's model and
tool boundaries into one linear ``RunCassette``; a supervised run spawns N subagent
runners (concurrently under fan-out) plus git worktree/merge side effects outside
the recorded boundaries, so a single shared cassette would interleave
nondeterministically and could not honestly replay. Rather than fake parity, the
branch logs a WARNING and records nothing; per-subagent cassettes need the durable
child ``agent_run`` linkage that is still parked (see
``agent_runner._maybe_persist_recording``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from forge_agent import AgentRunner
from forge_contracts import AgentObjective, AgentRunResult, ModelClient
from forge_coordinator import (
    CoordinatorDeps,
    CoordinatorSettings,
    InMemorySubAgentRunSink,
    SqlAlchemySubAgentRunSink,
    SubAgentRunSink,
    Supervisor,
)
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_db.session import create_session_factory

__all__ = ["run_supervised_objective"]

logger = logging.getLogger("forge.multi_agent")

#: A zero-arg callable yielding a (context-manager) SQLAlchemy session.
SessionFactory = Callable[[], Any]


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _with_parent_run_id(objective: AgentObjective) -> AgentObjective:
    """Pin ``context.parent_agent_run_id`` so persisted rows and the returned
    ``result.run_id`` share one identity (the Supervisor would otherwise mint an
    id internally that this adapter could not have created the parent row for)."""
    if _as_uuid(objective.context.get("parent_agent_run_id")) is not None:
        return objective
    context = {**objective.context, "parent_agent_run_id": str(uuid.uuid4())}
    return objective.model_copy(update={"context": context})


def _ensure_parent_agent_run(
    session_factory: SessionFactory,
    *,
    parent_id: uuid.UUID,
    workspace_id: uuid.UUID,
    objective: AgentObjective,
) -> None:
    """Insert the supervisor's own ``agent_run`` row if it does not exist yet.

    Every ``sub_agent_run`` row FKs ``parent_agent_run_id -> agent_run.id``, so
    the parent row must exist before the graph dispatches (the coordinator's own
    Postgres suite seeds exactly this row). ``task_id`` is intentionally left
    NULL: the objective's task uuid is not guaranteed to exist in ``task``.
    """
    from forge_db.models import AgentRun

    with session_factory() as session:
        if session.get(AgentRun, parent_id) is not None:
            return
        session.add(
            AgentRun(
                id=parent_id,
                workspace_id=workspace_id,
                role="primary",
                is_supervisor=True,
                status=DbRunStatus.RUNNING,
                inputs={
                    "objective": objective.objective,
                    "execution_mode": objective.execution_mode.value,
                    "key": objective.key,
                },
                started_at=datetime.now(UTC),
            )
        )
        session.commit()


def _finalize_parent_agent_run(session_factory: SessionFactory, *, result: AgentRunResult) -> None:
    """Best-effort: mirror the final supervised status onto the parent row.

    Never raises — the run's contract is the returned ``AgentRunResult``; the
    row update is durable observability on top (same fail-open stance as the
    recording sink).
    """
    from forge_db.models import AgentRun

    if result.run_id is None:  # pragma: no cover - finalize always sets run_id
        return
    try:
        with session_factory() as session:
            row = session.get(AgentRun, result.run_id)
            if row is None:  # pragma: no cover - ensured before the run
                return
            row.status = DbRunStatus(result.status.value)
            row.pattern = result.artifacts.get("pattern")
            row.confidence = result.confidence
            row.output = {"summary": result.summary, "output": result.output}
            row.completed_at = datetime.now(UTC)
            session.commit()
    except Exception:
        logger.exception("failed to finalize supervisor agent_run row (non-fatal)")


def _resolve_sub_agent_sink(
    objective: AgentObjective,
) -> tuple[SubAgentRunSink, SessionFactory | None]:
    """The durable SQL sink when DB + identity allow it; else in-memory (loud)."""
    workspace_id = _as_uuid(objective.context.get("workspace_id"))
    parent_id = _as_uuid(objective.context.get("parent_agent_run_id"))
    if workspace_id is None or parent_id is None:
        logger.warning(
            "supervised run has no context.workspace_id/parent_agent_run_id; "
            "sub_agent_run rows will NOT be durable (in-memory sink)"
        )
        return InMemorySubAgentRunSink(), None
    try:
        session_factory = create_session_factory()
        _ensure_parent_agent_run(
            session_factory,
            parent_id=parent_id,
            workspace_id=workspace_id,
            objective=objective,
        )
        return SqlAlchemySubAgentRunSink(session_factory), session_factory
    except Exception:
        logger.warning(
            "database unavailable for supervised-run persistence; sub_agent_run "
            "rows will NOT be durable (in-memory sink)",
            exc_info=True,
        )
        return InMemorySubAgentRunSink(), None


def _agent_factory(default_client: ModelClient) -> Callable[[ModelClient | None], AgentRunner]:
    """Build one subagent runtime per dispatch, on the per-role client when the
    coordinator routed one, else the run's shared default client."""

    def factory(model_client: ModelClient | None = None) -> AgentRunner:
        return AgentRunner(model_client or default_client)

    return factory


def run_supervised_objective(
    payload: dict[str, Any] | AgentObjective,
    *,
    model_client: ModelClient | None = None,
    sub_agent_sink: SubAgentRunSink | None = None,
    settings: CoordinatorSettings | None = None,
) -> AgentRunResult:
    """Run a supervised multi-agent objective through the F27 Supervisor.

    Keyword seams exist for tests/DI only; the Celery task calls this with just
    the payload. Settings come from the ``MULTI_AGENT_*`` environment — with
    ``MULTI_AGENT_ENABLED`` unset the coordinator's own gate escalates the run
    with ``needs_human_reason=multi_agent_disabled`` (never a silent
    single-agent fallback).
    """
    # Local import: agent_runner lazily imports this module inside the task body,
    # and this module needs its Task-1 model-client seam — resolve at call time.
    from forge_worker.agent_runner import _recording_enabled, _resolve_model_client

    objective = (
        payload if isinstance(payload, AgentObjective) else AgentObjective.model_validate(payload)
    )
    objective = _with_parent_run_id(objective)

    if _recording_enabled():
        logger.warning(
            "FORGE_RECORD_RUNS=1 is not supported on the supervised multi-agent "
            "path; this run will NOT be recorded (single-cassette record/replay "
            "cannot honestly capture N concurrent subagent runners — see the "
            "forge_worker.multi_agent module docstring)"
        )

    session_factory: SessionFactory | None = None
    if sub_agent_sink is None:
        sub_agent_sink, session_factory = _resolve_sub_agent_sink(objective)

    client = _resolve_model_client(model_client)
    deps = CoordinatorDeps(
        agent_factory=_agent_factory(client),
        sub_agent_sink=sub_agent_sink,
        settings=settings or CoordinatorSettings.from_env(),
    )
    result = Supervisor(deps).run(objective)
    if session_factory is not None:
        _finalize_parent_agent_run(session_factory, result=result)
    return result
