"""One agent run in its own process: ``python -m scf_worker.run_main RUN_ID WORKSPACE_ID``.

The process runs the loop of exactly one run (:func:`scf_worker.agent_runner.execute_run`,
or the supervisor for a supervised objective) and exits (#238). The worker keeps
the steps that need privileged credentials: preparing the repository before the
loop, and delivery, terminal recording and tracker write-back after it (spec 011
FR-011, FR-012). The loop's result is recorded as a handoff on the run, leaving the
run's status and ``completed_at`` to close-out.

The process reads the run's stored inputs, appends its events and records its
handoff only through a :class:`~scf_worker.run_record.RunRecord`; it opens no
database session itself. Per the owner decision of 2026-11-15 a run process reaches
its record with a short-lived token scoped to its own run, against SCF's API, and
never holds the database credential. That implementation is a follow-up: until it
exists the entry point runs only with the in-process implementation, in tests and
inline (``main(..., record=InProcessRunRecord-factory)``), and from the command
line it exits non-zero with no handoff. Nothing launches it yet (#239).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import FrameType

from celery.exceptions import SoftTimeLimitExceeded

from scf_contracts import AgentObjective, AgentRunResult, ExecutionMode, RunStatus
from scf_coordinator import InMemorySubAgentRunSink
from scf_worker.agent_runner import PreparedRun, execute_run, failure_result
from scf_worker.dispatch import RepositoryUnavailable, split_repo_name, workspace_mirrors
from scf_worker.multi_agent import run_supervised_objective
from scf_worker.run_record import RunRecord

__all__ = [
    "RUN_PROCESS_ALLOWED_ENV",
    "RUN_PROCESS_DENIED_ENV",
    "RUN_TIME_LIMIT_ENV",
    "RunTimeLimitExceeded",
    "execute",
    "existing_mirror",
    "main",
]

#: The run's wall-clock limit in seconds. A run process has no Celery time limit.
RUN_TIME_LIMIT_ENV = "FORGE_RUN_TIME_LIMIT"
DEFAULT_RUN_TIME_LIMIT = 3600.0

#: What a run process may receive: model access, the loop's own settings, the
#: sandbox backend and the mirrors the worker prepared (FR-011).
RUN_PROCESS_ALLOWED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        RUN_TIME_LIMIT_ENV,
        "FORGE_REPO_MIRRORS_DIR",
        "FORGE_AGENT_RUN_TOKEN_BUDGET",
        "FORGE_ALLOW_SCRIPTED_AGENT",
        "FORGE_MODEL_PROVIDER",
        "FORGE_MODEL_NAME",
        "FORGE_MODEL_API_KEY",
        "FORGE_MODEL_BASE_URL",
        "FORGE_MODEL_EFFORT",
        "FORGE_MODEL_MAX_RETRIES",
        "FORGE_MODEL_MAX_TOKENS",
        "FORGE_MODEL_PROMPT_CACHE",
        "FORGE_MODEL_TIMEOUT_S",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "FORGE_CLI_BRIDGE_URL",
        "FORGE_CLI_BRIDGE_TOKEN",
        "FORGE_LOCAL_CLI_ENABLED",
        "FORGE_LOCAL_CLI_MODEL",
        "FORGE_LOCAL_CLI_PREFERENCE",
        "FORGE_LOCAL_CLI_TIMEOUT_S",
        "FORGE_SANDBOX_ALLOWED_IMAGES",
        "FORGE_SANDBOX_CPUS",
        "FORGE_SANDBOX_DOCKER_HOST",
        "FORGE_SANDBOX_EGRESS_ALLOWLIST",
        "FORGE_SANDBOX_EGRESS_NETWORK",
        "FORGE_SANDBOX_EXEC_TIMEOUT_SECONDS",
        "FORGE_SANDBOX_GVISOR_PLATFORM",
        "FORGE_SANDBOX_GVISOR_RUNTIME",
        "FORGE_SANDBOX_IMAGE_GO",
        "FORGE_SANDBOX_IMAGE_NODE",
        "FORGE_SANDBOX_IMAGE_PYTHON",
        "FORGE_SANDBOX_JAILER_ROOT",
        "FORGE_SANDBOX_KIND",
        "FORGE_SANDBOX_MAX_TTL_SECONDS",
        "FORGE_SANDBOX_MEMORY_MB",
        "FORGE_SANDBOX_MICROVM_MEMORY_MB",
        "FORGE_SANDBOX_MICROVM_RUNTIME",
        "FORGE_SANDBOX_MICROVM_VCPUS",
        "FORGE_SANDBOX_NETWORK",
        "FORGE_SANDBOX_OUTPUT_CAP_BYTES",
        "FORGE_SANDBOX_PIDS_LIMIT",
        "FORGE_SANDBOX_REAP_INTERVAL_SECONDS",
        "FORGE_SANDBOX_REQUIRE_KVM",
        "FORGE_SANDBOX_RUN_GID",
        "FORGE_SANDBOX_RUN_UID",
        "FORGE_SANDBOX_TMPFS_MB",
        "FORGE_SANDBOX_WORKTREE_VOLUME",
        "MULTI_AGENT_CONFIDENCE_THRESHOLD",
        "MULTI_AGENT_ENABLED",
        "MULTI_AGENT_FALLBACK_TO_SINGLE_AGENT",
        "MULTI_AGENT_MAX_PARALLEL_CAP",
        "MULTI_AGENT_REVIEW_LOOP_BUDGET",
        "MULTI_AGENT_SUBAGENT_TIMEOUT_SECONDS",
    }
)

#: What a run process must never hold; it refuses to start when one is set (FR-011).
RUN_PROCESS_DENIED_ENV = frozenset(
    {
        # The source-control application key and identifiers.
        "FORGE_GITHUB_APP_ID",
        "FORGE_GITHUB_INSTALLATION_ID",
        "FORGE_GITHUB_APP_PRIVATE_KEY_PATH",
        # The master secret key, also as a mounted file and under its legacy name.
        "FORGE_SECRET_KEY",
        "FORGE_SECRET_KEY_FILE",
        "SECRET_KEY",
        # Webhook signing secrets.
        "FORGE_GITHUB_WEBHOOK_SECRET",
        "FORGE_SLACK_SIGNING_SECRET",
        "FORGE_PAGERDUTY_WEBHOOK_SECRET",
        "FORGE_DATADOG_WEBHOOK_SECRET",
        "FORGE_SENTRY_WEBHOOK_SECRET",
        "FORGE_GRAFANA_WEBHOOK_SECRET",
        # The database credential.
        "FORGE_DATABASE_URL",
    }
)

RecordFactory = Callable[[uuid.UUID, uuid.UUID], RunRecord]


class RunTimeLimitExceeded(BaseException):
    """The run's wall-clock limit passed.

    A ``BaseException``, so the loop's own ``except Exception`` (a tool's failure
    becomes its result) cannot swallow it and keep the run going.
    """


@contextmanager
def _wall_clock(seconds: float) -> Iterator[None]:
    """Raise :class:`RunTimeLimitExceeded` in the loop once ``seconds`` pass."""

    def expire(_signum: int, _frame: FrameType | None) -> None:
        raise RunTimeLimitExceeded

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def existing_mirror(objective: AgentObjective) -> AgentObjective:
    """The workspace's mirror as the worker left it, found without credentials.

    The path :func:`~scf_worker.dispatch.resolve_run_repository` derives; the worker
    fetched the mirror before the launch, so the loop never reads source-control
    application settings.
    """
    context = objective.context
    full_name = context.get("repo_full_name")
    if not full_name:
        return objective
    try:
        owner, name = split_repo_name(str(full_name))
    except ValueError as exc:
        raise RepositoryUnavailable(str(exc)) from exc
    path = workspace_mirrors(uuid.UUID(str(context["workspace_id"]))) / owner / name
    if not (path / "HEAD").is_file():
        raise RepositoryUnavailable(f"no mirror of {full_name} for this workspace")
    return objective.model_copy(update={"context": {**context, "repo_path": str(path)}})


def execute(
    record: RunRecord, run_id: uuid.UUID, workspace_id: uuid.UUID, *, time_limit: float
) -> AgentRunResult:
    """Run one run's loop from its stored inputs; a failure becomes the result."""
    task_id = None
    try:
        inputs = record.inputs()
        if inputs is None:
            raise LookupError("the run is not in this workspace")
        objective = AgentObjective.model_validate(inputs)
        task_id = objective.task_id
        # The run's identity is the launch's, whatever the stored inputs say.
        context = {
            **objective.context,
            "agent_run_id": str(run_id),
            "workspace_id": str(workspace_id),
        }
        objective = objective.model_copy(update={"context": context})
        with _wall_clock(time_limit):
            if objective.execution_mode is ExecutionMode.SUPERVISED_MULTI_AGENT:
                # Specialist rows stay in memory: the record port does not carry them.
                return run_supervised_objective(
                    objective,
                    sub_agent_sink=InMemorySubAgentRunSink(),
                    resolve_repository=existing_mirror,
                )
            run = PreparedRun(existing_mirror(objective), run_id, workspace_id, True, record)
            # Recordings are stored through the database, which this process never reaches.
            return execute_run(run, persist_recording=False)
    except RunTimeLimitExceeded:  # the same marker as the Celery path's soft time limit
        return failure_result(run_id, task_id, SoftTimeLimitExceeded())
    except RepositoryUnavailable as exc:
        return AgentRunResult(
            run_id=run_id, task_id=task_id, status=RunStatus.FAILED, error=str(exc)
        )
    except Exception as exc:
        return failure_result(run_id, task_id, exc)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    record: RecordFactory | None = None,
) -> int:
    """Run one run's loop and record its handoff: 0 when a handoff was recorded.

    ``env`` is the process environment the denied names and the time limit are read
    from (``os.environ`` by default). ``record`` builds the run's record port.
    """
    environ = os.environ if env is None else env
    held = sorted(RUN_PROCESS_DENIED_ENV.intersection(environ))
    if held:
        # Names only: the values are secrets.
        print(f"run process refused to start: it must not hold {', '.join(held)}", file=sys.stderr)
        return 2
    try:
        time_limit = float(environ.get(RUN_TIME_LIMIT_ENV) or DEFAULT_RUN_TIME_LIMIT)
    except ValueError:
        time_limit = 0.0
    if not time_limit > 0:
        print(
            f"run process refused to start: {RUN_TIME_LIMIT_ENV} must be seconds > 0",
            file=sys.stderr,
        )
        return 2
    parser = argparse.ArgumentParser(prog="python -m scf_worker.run_main")
    parser.add_argument("run_id", type=uuid.UUID)
    parser.add_argument("workspace_id", type=uuid.UUID)
    args = parser.parse_args(argv)
    if record is None:
        print(
            "no run record is reachable: a run process never holds the database "
            "credential, and the API-backed record is not implemented yet",
            file=sys.stderr,
        )
        return 1
    port = record(args.run_id, args.workspace_id)
    result = execute(port, args.run_id, args.workspace_id, time_limit=time_limit)
    return 0 if port.record_handoff(result) else 1


if __name__ == "__main__":
    sys.exit(main())
