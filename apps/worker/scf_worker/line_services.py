"""The bodies of a line's service stations (spec K §4.1, issue #181).

:func:`line_services` returns one body per phase-1 catalog name; the worker's
composition root binds them to ``forge.run_service_station``. Each body finds the
run's repository through its task in the database, never through an agent's output
(ADR-0014), and checks every ``trust: task`` input against that task. They work on
the run's own branch, ``forge/run-<hex>``, in the workspace mirror. None integrates a
change into the base branch: they push only the run branch, open it as a draft pull
request, wait for its CI and mark the pull request ready for a person.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from scf_api.services.agent_dispatch import repo_connected
from scf_api.services.pm_service import PMConnectionService
from scf_contracts import AgentObjective
from scf_contracts.lines import OpenPrParams
from scf_db.models import Task
from scf_db.models.pm import PMTaskLink
from scf_integrations.github import GitHubClient
from scf_worker.dispatch import (
    MIRROR_TOKEN_PERMISSIONS,
    ensure_mirror,
    split_repo_name,
    workspace_mirrors,
)
from scf_worker.finalizer import (
    RUN_TOKEN_PERMISSIONS,
    GitHubAppCredentials,
    Pusher,
    git_push,
    mint_token,
    push_and_open_pr,
    workspace_credentials,
)
from scf_worker.review_rules import default_branch_policy
from scf_worker.run_workspace import CheckRun, _git_out, run_branch, run_declared_checks
from scf_worker.tasks.pm_dispatch import (
    _connection_service,
    _post_writeback,
    _set_state_writeback,
)
from scf_worker.temporal_services import _CI_PASSING, _owned_run
from scf_workflow.temporal.activities import ServiceFn, heartbeat_from_service, service_cancelled
from scf_workflow.temporal.payloads import ServiceStationInput, ServiceStationResult

#: A line run's branch is ``forge/run-<hex>`` (spec K §4.1); merge bookkeeping keys on it.
BRANCH_PREFIX = "forge"
#: Confirm-red runs the policy's test command, declared under one of these names.
TEST_COMMANDS = ("test", "tests")
#: A ``failure_summary`` keeps its last bytes; a visit's outputs are capped at 16 KiB.
SUMMARY_MAX_BYTES = 8 * 1024
#: The CI wait reads the pull request and its head commit's check runs, nothing else.
CI_PERMISSIONS = {"checks": "read", "pull_requests": "read"}

Credentials = Callable[[uuid.UUID, str], GitHubAppCredentials | None]


@dataclass(frozen=True)
class CiPolling:
    """How ``forge.await_ci`` polls the head commit's check runs."""

    poll_seconds: float = 30.0
    #: Zero check runs this long after the first poll is ``no_checks``, never green.
    no_checks_after: float = 300.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    heartbeat: Callable[..., None] = heartbeat_from_service
    cancelled: Callable[[], bool] = service_cancelled


@dataclass(frozen=True)
class _Run:
    task_id: uuid.UUID
    title: str
    repo: str
    base: str
    mirror: Path
    branch: str

    def head(self) -> str:
        return _git_out(self.mirror, "rev-parse", f"refs/heads/{self.branch}").strip()


def _failed(code: str, message: str = "") -> ServiceStationResult:
    return ServiceStationResult("failed", error_code=code, error_message=message[:500] or None)


def _summary(text: str) -> dict[str, Any]:
    return {"failure_summary": text.encode()[-SUMMARY_MAX_BYTES:].decode(errors="ignore")}


def _ci_failure(run: dict[str, Any]) -> str:
    summary = (run.get("output") or {}).get("summary")
    return f"{run.get('name')}: {run.get('conclusion')}" + (f"\n{summary}" if summary else "")


def run_worktree(mirror: Path, branch: str) -> Path | None:
    """The worktree of ``mirror`` checked out on ``branch``, if there is one."""
    for entry in _git_out(mirror, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in entry.splitlines())
        if fields.get("branch") == f"refs/heads/{branch}":
            return Path(fields["worktree"])
    return None


def line_services(
    factory: sessionmaker[Session],
    *,
    credentials: Credentials | None = None,
    transport: httpx.BaseTransport | None = None,
    push: Pusher = git_push,
    connections: PMConnectionService | None = None,
    ci: CiPolling | None = None,
) -> dict[str, ServiceFn]:
    """One body per phase-1 catalog service (spec K §4.1), by catalog name.

    ``credentials``, ``transport`` and ``connections`` stand in for the workspace's
    GitHub App credentials, GitHub's HTTP layer and its tracker connections.
    """
    polling = ci or CiPolling()

    def run_of(inp: ServiceStationInput) -> _Run:
        with factory() as session:
            run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            task = session.get(Task, run.task_id)
            assert task is not None  # workflow_run.task_id is a non-null foreign key
            target = AgentObjective(
                objective=task.title, repo_targets=task.repo_targets
            ).primary_repo_target
            task_id, title = task.id, f"{task.key + ': ' if task.key else ''}{task.title}"
            if target is None:
                raise ApplicationError(
                    f"task {task_id} names no repository",
                    type="RepoTargetMissing",
                    non_retryable=True,
                )
            # As on every other run path: never a repository the workspace has not connected.
            if not repo_connected(session, inp.workspace_id, target.repo):
                raise ApplicationError(
                    f"repository {target.repo!r} is not connected to this workspace",
                    type="RepoNotConnected",
                    non_retryable=True,
                )
        owner, name = split_repo_name(target.repo)
        return _Run(
            task_id=task_id,
            title=title[:120],
            repo=target.repo,
            base=target.base_branch,
            mirror=workspace_mirrors(inp.workspace_id) / owner / name,
            branch=run_branch(BRANCH_PREFIX, inp.workflow_run_id),
        )

    def creds_of(inp: ServiceStationInput, repo: str) -> GitHubAppCredentials | None:
        if credentials is not None:
            return credentials(inp.workspace_id, repo)
        return workspace_credentials(inp.workspace_id, repo, session_factory=factory)

    def required_creds(inp: ServiceStationInput, repo: str) -> GitHubAppCredentials:
        creds = creds_of(inp, repo)
        if creds is None:
            raise ApplicationError(
                "this workspace has no GitHub App credentials",
                type="GitHubAppNotConfigured",
                non_retryable=True,
            )
        return creds

    def token(inp: ServiceStationInput, creds: GitHubAppCredentials, run: _Run, perms: dict) -> str:
        """A token for ``run.repo`` alone with ``perms``, audited (ADR-0008)."""
        return mint_token(
            creds,
            run.repo,
            perms,
            workspace_id=inp.workspace_id,
            run_id=inp.workflow_run_id,
            transport=transport,
            session_factory=factory,
        )

    def client_of(creds: GitHubAppCredentials, secret: str) -> GitHubClient:
        return GitHubClient(token=secret, base_url=creds.api_base_url, transport=transport)

    def run_pr(
        inp: ServiceStationInput, run: _Run, perms: dict
    ) -> tuple[int, GitHubAppCredentials, str] | ServiceStationResult:
        """The number of the pull request ``pr`` names, the credentials and a token.

        Refused (``ref_refused``) unless it is in the task's repository and on the run's
        own branch: a body never acts on any other ref.
        """
        repo, _, number = str(inp.inputs.get("pr", "")).rpartition("#")
        if repo != run.repo or not number.isdigit():
            return _failed("ref_refused", "the pr input names no pull request of the task")
        creds = required_creds(inp, run.repo)
        secret = token(inp, creds, run, perms)
        with client_of(creds, secret) as client:
            if client.get_pr(run.repo, int(number)).head != run.branch:
                return _failed("ref_refused", f"pull request {number} is not on {run.branch}")
        return int(number), creds, secret

    def prepare_worktree(inp: ServiceStationInput) -> ServiceStationResult:
        """The run's one worktree on its branch, from a freshly fetched base (idempotent)."""
        run = run_of(inp)
        if inp.inputs.get("repo") != run.repo:
            return _failed("repo_mismatch", "the repo input is not the task's repository")
        creds = creds_of(inp, run.repo)
        if creds is not None:
            ensure_mirror(
                run.repo,
                root=workspace_mirrors(inp.workspace_id),
                remote_url=f"{creds.git_base_url}/{run.repo}.git",
                token=token(inp, creds, run, MIRROR_TOKEN_PERMISSIONS),
            )
        elif not (run.mirror / "HEAD").is_file():
            return _failed("repository_unavailable", f"no mirror of {run.repo} to work in")
        if run_worktree(run.mirror, run.branch) is None:
            # Removed when the run finishes or is cancelled; the holder's prefix is what
            # that cleanup removes.
            tree = str(Path(tempfile.mkdtemp(prefix="forge-wt-")) / "tree")
            exists = not subprocess.run(
                ["git", "-C", str(run.mirror), "rev-parse", "-q", "--verify", run.branch],
                capture_output=True,
            ).returncode
            args = [tree, run.branch] if exists else ["-b", run.branch, tree, run.base]
            _git_out(run.mirror, "worktree", "add", *args)
        return ServiceStationResult()

    def checks(inp: ServiceStationInput, run: _Run, commit: str, policy: Any) -> list[CheckRun]:
        return run_declared_checks(
            run.mirror, commit, policy, run_id=inp.workflow_run_id, workspace_id=inp.workspace_id
        )

    def confirm_red(inp: ServiceStationInput) -> ServiceStationResult:
        """``succeeded`` only when the tests fail on the run branch and pass on its base.

        The declared test command runs on both commits; test names are never parsed.
        """
        run = run_of(inp)
        try:  # the default branch's policy: an agent cannot change its own checks
            policy = default_branch_policy(run.mirror)
        except (ValueError, yaml.YAMLError) as exc:
            return _failed("policy_invalid", str(exc))
        commands = policy.commands if policy else {}
        name = next((n for n in TEST_COMMANDS if commands.get(n)), None)
        if policy is None or name is None:
            return _failed("no_test_command", f"the policy declares none of {TEST_COMMANDS}")
        tests = policy.model_copy(update={"commands": {name: commands[name]}})
        head = run.head()
        [on_base] = checks(
            inp, run, _git_out(run.mirror, "merge-base", run.base, head).strip(), tests
        )
        if not on_base.passed:
            return _failed("base_not_green", on_base.output_tail[-500:])
        [on_head] = checks(inp, run, head, tests)
        return ServiceStationResult("not_red" if on_head.passed else "succeeded")

    def local_checks(inp: ServiceStationInput) -> ServiceStationResult:
        """The policy's declared commands on a clean checkout of the run branch (#172)."""
        run = run_of(inp)
        try:
            policy = default_branch_policy(run.mirror)
        except (ValueError, yaml.YAMLError) as exc:
            return _failed("policy_invalid", str(exc))
        runs = checks(inp, run, run.head(), policy)
        if not runs:
            return _failed("no_checks_declared", "the policy declares no command")
        failing = [r for r in runs if not r.passed]
        if not failing:
            return ServiceStationResult()
        text = "\n\n".join(
            f"{r.id} (exit {r.exit_code}{', timed out' if r.timed_out else ''}):\n{r.output_tail}"
            for r in failing
        )
        return ServiceStationResult("checks_failed", outputs=_summary(text))

    def open_pr(inp: ServiceStationInput) -> ServiceStationResult:
        """The run branch's pull request, a draft by default, opened once (#170)."""
        run = run_of(inp)
        if inp.inputs.get("task") != str(run.task_id):
            return _failed("task_mismatch", "the task input is not the run's task")
        pr = push_and_open_pr(
            repo_path=run.mirror,
            branch=run.branch,
            repo_full_name=run.repo,
            base_branch=run.base,
            title=run.title,
            body=(
                f"Opened by SCF line run `{inp.workflow_run_id}` for task {run.task_id}. "
                "It stays a draft until its checks and CI pass; a person decides the merge."
            ),
            credentials=required_creds(inp, run.repo),
            workspace_id=inp.workspace_id,
            run_id=inp.workflow_run_id,
            session_factory=factory,
            transport=transport,
            push=push,
            reuse_existing=True,
            draft=OpenPrParams.model_validate(inp.params).draft,
        )
        return ServiceStationResult(outputs={"pr_ref": f"{run.repo}#{pr.number}"})

    def push_branch(inp: ServiceStationInput) -> ServiceStationResult:
        """Push the run branch, and only it, with a one-repository token."""
        run = run_of(inp)
        found = run_pr(inp, run, RUN_TOKEN_PERMISSIONS)
        if isinstance(found, ServiceStationResult):
            return found
        _, creds, secret = found
        push(run.mirror, run.branch, f"{creds.git_base_url}/{run.repo}.git", secret)
        return ServiceStationResult()

    def await_ci(inp: ServiceStationInput) -> ServiceStationResult:
        """Poll the run branch head's check runs, heartbeating each poll, until CI decides."""
        run = run_of(inp)
        found = run_pr(inp, run, CI_PERMISSIONS)
        if isinstance(found, ServiceStationResult):
            return found
        _, creds, secret = found
        head, started = run.head(), polling.clock()
        # shortcut: one token for the whole wait (an hour at most, as the station's timeout)
        with client_of(creds, secret) as client:
            while True:
                polling.heartbeat({"phase": "await_ci", "head": head})
                runs = client.check_runs_for_ref(run.repo, head)
                failed = [
                    r
                    for r in runs
                    if r.get("status") == "completed" and r.get("conclusion") not in _CI_PASSING
                ]
                if failed:
                    text = "\n".join(map(_ci_failure, failed))
                    return ServiceStationResult("checks_failed", outputs=_summary(text))
                if runs and all(r.get("status") == "completed" for r in runs):
                    # All skipped or neutral (CI gated off for drafts, say): nothing ran.
                    ran = any(r.get("conclusion") == "success" for r in runs)
                    return ServiceStationResult() if ran else ServiceStationResult("no_checks")
                if not runs and polling.clock() - started >= polling.no_checks_after:
                    return ServiceStationResult("no_checks")  # never green (lore #2361)
                polling.sleep(polling.poll_seconds)
                if polling.cancelled():
                    return _failed("cancelled", "the CI wait was cancelled")

    def mark_pr_ready(inp: ServiceStationInput) -> ServiceStationResult:
        run = run_of(inp)
        found = run_pr(inp, run, {"pull_requests": "write"})
        if isinstance(found, ServiceStationResult):
            return found
        number, creds, secret = found
        with client_of(creds, secret) as client:
            client.mark_pr_ready(run.repo, number)
        return ServiceStationResult()

    def tracker_comment(inp: ServiceStationInput) -> ServiceStationResult:
        """One comment on the run's task's tracker item; its status is never changed."""
        ticket = str(inp.inputs.get("ticket", ""))
        with factory() as session:
            run = _owned_run(session, inp.workflow_run_id, inp.workspace_id)
            link = session.scalars(
                select(PMTaskLink).where(
                    PMTaskLink.workspace_id == inp.workspace_id,
                    PMTaskLink.forge_task_id == run.task_id,
                    or_(PMTaskLink.external_id == ticket, PMTaskLink.external_key == ticket),
                )
            ).first()
            if link is None:
                return _failed("ticket_not_linked", "the ticket is not the run's task's item")
            tracker = {"connection_id": str(link.connection_id), "external_id": link.external_id}
        key = inp.idempotency_key  # one claim per visit: a retried visit posts nothing more
        posted = _post_writeback(
            claim=lambda: _set_state_writeback(
                factory, inp.workflow_run_id, key, {"status": "posting"}, claim=True
            ),
            record=lambda record: _set_state_writeback(factory, inp.workflow_run_id, key, record),
            run_id=inp.workflow_run_id,
            headline=str(inp.inputs.get("body", "")),
            pr_url=None,
            pr_opened=False,  # no status mapping applies: only the comment is posted
            workspace_id=inp.workspace_id,
            tracker=tracker,
            connections=connections or _connection_service(factory),
        )
        if posted in ("posted", "already_recorded"):
            return ServiceStationResult()
        return _failed(f"comment_{posted}", "the tracker comment was not posted")

    return {
        "forge.prepare_worktree": prepare_worktree,
        "forge.confirm_red": confirm_red,
        "forge.run_checks": local_checks,
        "forge.open_pr": open_pr,
        "forge.push_branch": push_branch,
        "forge.await_ci": await_ci,
        "forge.mark_pr_ready": mark_pr_ready,
        "forge.tracker_comment": tracker_comment,
    }


__all__ = ["BRANCH_PREFIX", "CiPolling", "line_services", "run_worktree"]
