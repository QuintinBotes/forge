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
never holds the database credential: with ``FORGE_RUN_TOKEN`` and
``FORGE_RUN_API_URL``, which the worker gives each unit it launches (#257), it uses
:class:`~scf_worker.api_run_record.ApiRunRecord`. Without either it runs only with
a record passed in (``main(..., record=...)``, in tests), and from the command line
it exits non-zero with no handoff.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from types import FrameType

from celery.exceptions import SoftTimeLimitExceeded

from scf_contracts import AgentObjective, AgentRunResult, ExecutionMode, RunStatus
from scf_coordinator import InMemorySubAgentRunSink
from scf_worker.agent_runner import PreparedRun, execute_run, failure_result
from scf_worker.api_run_record import ApiRunRecord, RunRecordError
from scf_worker.dispatch import RepositoryUnavailable, split_repo_name, workspace_mirrors
from scf_worker.multi_agent import run_supervised_objective
from scf_worker.run_record import RunRecord

__all__ = [
    "RUN_API_URL_ENV",
    "RUN_PROCESS_ALLOWED_ENV",
    "RUN_PROCESS_DENIED_ENV",
    "RUN_TIME_LIMIT_ENV",
    "RUN_TOKEN_ENV",
    "RunTimeLimitExceeded",
    "execute",
    "existing_mirror",
    "main",
]

#: The run's wall-clock limit in seconds. A run process has no Celery time limit.
RUN_TIME_LIMIT_ENV = "FORGE_RUN_TIME_LIMIT"
DEFAULT_RUN_TIME_LIMIT = 3600.0
#: The run's own token (issued per unit at launch) and the API address it is used against.
RUN_TOKEN_ENV = "FORGE_RUN_TOKEN"
RUN_API_URL_ENV = "FORGE_RUN_API_URL"

#: What a run process may receive: model access, its run record, the loop's own
#: settings, the sandbox backend and the mirrors the worker prepared (FR-011).
RUN_PROCESS_ALLOWED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        RUN_TIME_LIMIT_ENV,
        RUN_TOKEN_ENV,
        RUN_API_URL_ENV,
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
        # The Helm chart's names for these and the other platform secrets it shares
        # with every workload through one envFrom block.
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_CLIENT_SECRET",
        "GITHUB_APP_WEBHOOK_SECRET",
        "SLACK_SIGNING_SECRET",
        "SLACK_BOT_TOKEN",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "FORGE_VAULT_KEYS",
        "TEMPORAL_CODEC_KEY",
        "INTERNAL_SERVICE_TOKEN",
        "AUTH_SECRET",
        "API_KEY_PEPPER",
    }
)
#: Retained master keys are ``FORGE_SECRET_KEY_V<n>`` (``scf_api.auth.keyring``).
RUN_PROCESS_DENIED_PREFIXES = ("FORGE_SECRET_KEY_V",)
#: setitimer cannot hold much more; a week is far beyond any run.
MAX_RUN_TIME_LIMIT = 7 * 24 * 3600.0

RecordFactory = Callable[[uuid.UUID, uuid.UUID], RunRecord]


class RunTimeLimitExceeded(BaseException):
    """The run's wall-clock limit passed.

    A ``BaseException``, so the loop's own ``except Exception`` (a tool's failure
    becomes its result) cannot swallow it and keep the run going.
    """


@contextmanager
def _wall_clock(seconds: float) -> Iterator[None]:
    """Raise :class:`RunTimeLimitExceeded` in the loop once ``seconds`` pass.

    shortcut: a signal reaches only the main thread, so a supervised run's parallel
    children finish their current loop before the limit is seen; the executor that
    launches run processes (#239) is the hard stop.
    """

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

    ``env`` is the process environment the denied names, the time limit, the run
    token and the API address are read from (``os.environ`` by default). ``record``
    builds the run's record port; without one, the token and address select the API's.
    """
    environ = os.environ if env is None else env
    if threading.current_thread() is not threading.main_thread():
        print("run process refused to start: it must run on the main thread", file=sys.stderr)
        return 2
    held = sorted(
        name
        for name in environ
        if name in RUN_PROCESS_DENIED_ENV or name.startswith(RUN_PROCESS_DENIED_PREFIXES)
    )
    if held:
        # Names only: the values are secrets.
        print(f"run process refused to start: it must not hold {', '.join(held)}", file=sys.stderr)
        return 2
    try:
        time_limit = float(environ.get(RUN_TIME_LIMIT_ENV) or DEFAULT_RUN_TIME_LIMIT)
    except ValueError:
        time_limit = 0.0
    if not 0 < time_limit <= MAX_RUN_TIME_LIMIT:  # also refuses inf and nan
        print(
            f"run process refused to start: {RUN_TIME_LIMIT_ENV} must be seconds,"
            f" > 0 and at most {MAX_RUN_TIME_LIMIT:.0f}",
            file=sys.stderr,
        )
        return 2
    token, api_url = environ.get(RUN_TOKEN_ENV), environ.get(RUN_API_URL_ENV)
    if bool(token) != bool(api_url):
        missing = RUN_API_URL_ENV if token else RUN_TOKEN_ENV
        print(f"run process refused to start: {missing} is not set", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="python -m scf_worker.run_main")
    parser.add_argument("run_id", type=uuid.UUID)
    parser.add_argument("workspace_id", type=uuid.UUID)
    args = parser.parse_args(argv)
    if record is None and token and api_url:
        record = partial(ApiRunRecord, api_url=api_url, token=token)
    if record is None:
        print(
            "no run record is reachable: a run process never holds the database "
            f"credential, and the API-backed record needs {RUN_TOKEN_ENV} and {RUN_API_URL_ENV}",
            file=sys.stderr,
        )
        return 1
    port = record(args.run_id, args.workspace_id)
    result = execute(port, args.run_id, args.workspace_id, time_limit=time_limit)
    try:
        return 0 if port.record_handoff(result) else 1
    except RunRecordError as exc:  # the status or the transport error, never the token
        print(f"the handoff was not recorded: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
