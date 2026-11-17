"""Shared agent-run admission + tracker objective building (spec M M3).

One code path creates and enqueues an agent run, whether the caller is a
person hitting ``POST /agent/runs`` or the worker reacting to a tracker label
webhook (``scf_worker.tasks.pm_dispatch``):

* :func:`create_and_enqueue_run` — strip every server-owned context key, set
  identity server-side, refuse unconnected repositories, persist the
  ``AgentRun`` row *before* enqueueing, and mark it failed when the broker
  refuses. (Factored out of ``scf_api.routers.agent``.)
* :func:`build_tracker_objective` — build the objective from a *re-fetched*
  tracker item (never from a webhook payload): title, body truncated to
  20,000 characters, acceptance criteria parsed from a
  ``## Acceptance criteria`` section. Tracker text is untrusted (ADR-0014):
  it is labelled with its source in the run context and can never set a
  server-owned context key — the builder writes no tracker-derived context
  at all; provenance is returned separately as forge-set server context.
* :func:`resolve_tracker_ref` — resolve ``provider:key`` (e.g.
  ``github_issues:acme/widgets#42``) through the PM link store / connection,
  workspace-scoped; anything unknown or foreign is simply not found.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_api.settings import get_settings
from scf_contracts import (
    AcceptanceCriterion,
    AgentObjective,
    AgentRunResult,
    ExecutionMode,
    RepoTarget,
)
from scf_contracts.pm import ExternalTask
from scf_db.models import AgentRun, RepositoryConnection, Task
from scf_db.models.enums import PMProvider as DbPMProvider
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_db.models.pm import PMConnection, PMTaskLink
from scf_integrations.github import GitHubClient

logger = logging.getLogger("scf_api.services.agent_dispatch")

#: Run-context keys only the server sets; any caller-supplied value is dropped.
SERVER_OWNED_CONTEXT = frozenset(
    {
        "agent_run_id",
        "workspace_id",
        "parent_agent_run_id",
        "repo_path",
        "repo_full_name",
        "worktree_path",
        "role",
        "workflow_run_id",
        "failing_checks",
    }
)

#: Tracker provenance keys (spec M): likewise forge-set only, so neither an API
#: caller nor tracker text can forge a tracker identity, actor, or source.
TRACKER_CONTEXT_KEYS = frozenset(
    {"tracker", "source", "actor", "tracker_ref", "pm_connection_id", "sender_login"}
)

#: Every context key a caller may not set: SCF sets these itself.
STRIPPED_CONTEXT = SERVER_OWNED_CONTEXT | TRACKER_CONTEXT_KEYS
_STRIPPED_CONTEXT = STRIPPED_CONTEXT

#: Tracker issue bodies are untrusted and unbounded; the objective carries at
#: most this many characters of one.
MAX_TRACKER_BODY_CHARS = 20_000


class RepoNotConnected(ValueError):
    """The objective names a repository not connected to the workspace."""


class AgentEnqueueFailed(RuntimeError):
    """The persisted run could not be enqueued (row already marked failed)."""


class TrackerRefNotFound(LookupError):
    """A ``provider:key`` tracker ref is unknown in the caller's workspace."""


class TaskNotFound(LookupError):
    """The objective's board task is unknown in the caller's workspace."""


class GovernedRunRefused(ValueError):
    """A governed run cannot start; ``reason`` names why (#276)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UngovernedRunRefused(GovernedRunRefused):
    """A repository-changing run would run outside a workflow run, outside development."""

    def __init__(self, why: str) -> None:
        super().__init__(f"ungoverned_run_refused: {why}")


class DurableEngineUnavailable(RuntimeError):
    """The durable engine is selected but could not start the run: never a fallback."""


def ungoverned_runs_allowed() -> bool:
    """Only development may run a repository-changing run with no workflow run (#277).

    Owner decision 2026-11-15 (spec FR-005): when the durable engine is unavailable,
    production refuses the run; development runs it, labelled ungoverned.
    """
    return get_settings().environment == "development"


def refuse_ungoverned(objective: AgentObjective) -> None:
    """Raise ``UngovernedRunRefused`` for a repository-changing run outside development."""
    if objective.primary_repo_target is None or ungoverned_runs_allowed():
        return
    if objective.execution_mode == ExecutionMode.SUPERVISED_MULTI_AGENT:
        raise UngovernedRunRefused("supervised runs are not governed")
    raise UngovernedRunRefused("the durable engine is not selected")


def repo_connected(session: Session, workspace_id: uuid.UUID, repo: str) -> bool:
    """An active repository connection in this workspace names ``repo``."""
    row = session.scalar(
        select(RepositoryConnection.id).where(
            RepositoryConnection.workspace_id == workspace_id,
            RepositoryConnection.is_active.is_(True),
            (RepositoryConnection.repo_id == repo) | (RepositoryConnection.name == repo),
        )
    )
    return row is not None


def create_and_enqueue_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    objective: AgentObjective,
    enqueue: Callable[[AgentObjective], None],
    server_context: dict | None = None,
) -> AgentRunResult:
    """Persist an ``AgentRun`` for ``objective`` and enqueue it.

    Trust boundary: paths, identities, roles and tracker provenance in the run
    context are the server's to set. A caller-supplied ``repo_path`` or
    ``worktree_path`` would let a run act on another tenant's mirror or
    anywhere on the worker host; a forged ``tracker`` block would let tracker
    text drive write-back to an issue it does not own. ``server_context`` is
    forge-set provenance applied after stripping.

    This is the direct path: outside development a repository-changing run is
    refused here (``UngovernedRunRefused``), before anything is persisted (#277).
    """
    refuse_ungoverned(objective)
    run_id = uuid.uuid4()
    context = {k: v for k, v in objective.context.items() if k not in _STRIPPED_CONTEXT}
    context |= {"agent_run_id": str(run_id), "workspace_id": str(workspace_id)}
    if server_context:
        context |= server_context
    for target in objective.repo_targets:
        if not repo_connected(session, workspace_id, target.repo):
            raise RepoNotConnected(target.repo)
    if objective.primary_repo_target is not None:
        context["repo_full_name"] = objective.primary_repo_target.repo
    if objective.execution_mode == ExecutionMode.SUPERVISED_MULTI_AGENT:
        context["parent_agent_run_id"] = str(run_id)
    objective = objective.model_copy(update={"context": context})
    row = AgentRun(
        id=run_id,
        workspace_id=workspace_id,
        inputs=objective.model_dump(mode="json"),
        skill_profile=objective.skill_profile.name if objective.skill_profile else None,
        is_supervisor=objective.execution_mode == ExecutionMode.SUPERVISED_MULTI_AGENT,
        status=DbRunStatus.PENDING,
    )
    session.add(row)
    # A fast worker must see the row before it consumes the message.
    session.commit()
    try:
        enqueue(objective)
    except Exception as exc:
        row.status = DbRunStatus.FAILED
        row.output = {"error": "enqueue_failed"}
        session.commit()
        logger.exception("failed to enqueue agent run %s", run_id)
        raise AgentEnqueueFailed(str(run_id)) from exc
    return AgentRunResult(run_id=run_id, task_id=objective.task_id)


def claim_task_repository(task: Task, objective: AgentObjective) -> bool:
    """Make ``task`` name the objective's repository; ``False`` when it names another.

    Every step of a governed run (the guard, the agent, the checks, the pull
    request) reads the task's repository, and a task with none gets the objective's.
    The caller commits.
    """
    target = objective.primary_repo_target
    if target is None:
        return True
    current = AgentObjective(objective="claim", repo_targets=task.repo_targets)
    if current.primary_repo_target is None:
        task.repo_targets = [target.model_dump(mode="json")]
        return True
    return current.primary_repo_target.repo.lower() == target.repo.lower()


#: What a governed run's agent step takes from the kept objective; the task sets the
#: rest (its model, actions and execution mode). See ``agent_run_service`` (#276).
_GOVERNED_FIELDS = frozenset(
    {"task_id", "key", "objective", "description", "acceptance_criteria", "repo_targets", "context"}
)


def _unsupported_settings(objective: AgentObjective, task: Task) -> list[str]:
    """The settings a governed run's agent would not apply, so must not be accepted."""
    unsupported = set(objective.model_dump(exclude_defaults=True)) - _GOVERNED_FIELDS
    targets = objective.repo_targets
    if len(targets) > 1 or (targets and targets[0].skill_profile):
        unsupported.add("repo_targets")  # one repository, with the task's skill profile
    if task.execution_mode == ExecutionMode.SUPERVISED_MULTI_AGENT:
        unsupported.add("execution_mode")  # the agent step refuses it (ADR-0007)
    return sorted(unsupported)


def start_governed_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    objective: AgentObjective,
    started_by: str,
    server_context: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Start the workflow run a repository-changing objective runs inside (#276).

    The agent runs in the workflow, after its gates, on the objective's text kept
    on the run (as issue dispatch keeps it, #171) and the task's settings. A
    request that sets anything else is refused rather than accepted and dropped.
    Returns the workflow run's id and the id its first agent step will take.
    Server-owned and tracker context keys are stripped: ``server_context`` is
    SCF's own provenance for a tracker reference.
    """
    from scf_api.services.workflow_engine import select_workflow_engine
    from scf_workflow.exceptions import DuplicateRunError
    from scf_workflow.temporal.engine import TemporalWorkflowEngine
    from scf_workflow.temporal.ids import agent_run_id

    if objective.task_id is None:
        raise GovernedRunRefused("governed_run_requires_task")
    task = session.get(Task, objective.task_id)
    if task is None or task.workspace_id != workspace_id:
        raise TaskNotFound(str(objective.task_id))
    unsupported = _unsupported_settings(objective, task)
    if unsupported:
        raise GovernedRunRefused(f"governed_run_unsupported: {', '.join(unsupported)}")
    for target in objective.repo_targets:
        if not repo_connected(session, workspace_id, target.repo):
            raise RepoNotConnected(target.repo)
    if not claim_task_repository(task, objective):
        raise GovernedRunRefused("task_repository_mismatch")
    own = {k: v for k, v in objective.context.items() if k not in STRIPPED_CONTEXT}
    trigger: dict[str, object] = {
        "source": "api",
        "started_by": started_by,
        "objective": objective.model_copy(update={"context": own}).model_dump(mode="json"),
    }
    if server_context:
        # The agent renders the tracker item's text as untrusted data under this label.
        trigger["source_label"] = server_context.get("source")
        trigger["tracker"] = server_context.get("tracker")
    engine = select_workflow_engine(session=session, workspace_id=workspace_id)
    assert isinstance(engine, TemporalWorkflowEngine)  # the caller selected the durable engine
    try:
        run = engine.start(task.id, trigger=trigger, started_by=started_by)
    except DuplicateRunError as exc:
        raise GovernedRunRefused("governed_run_active") from exc
    except Exception as exc:  # refuse, never fall back: an unreachable server leaves no row
        logger.exception("the durable engine could not start a run for task %s", task.id)
        raise DurableEngineUnavailable(str(task.id)) from exc
    session.commit()
    assert run.id is not None  # the engine assigns it on start
    return run.id, agent_run_id(run.id)


# --------------------------------------------------------------------------- #
# Tracker objective building                                                  #
# --------------------------------------------------------------------------- #

_AC_HEADING = re.compile(r"^##\s+Acceptance criteria\s*$", re.IGNORECASE)
_NEXT_HEADING = re.compile(r"^##\s+")
_AC_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")


def parse_acceptance_criteria(body: str) -> list[AcceptanceCriterion]:
    """Bullet/numbered items under a ``## Acceptance criteria`` heading."""
    criteria: list[AcceptanceCriterion] = []
    in_section = False
    for line in body.splitlines():
        if _AC_HEADING.match(line):
            in_section = True
            continue
        if in_section and _NEXT_HEADING.match(line):
            break
        if not in_section:
            continue
        match = _AC_ITEM.match(line)
        if match and match.group(1):
            criteria.append(AcceptanceCriterion(id=f"AC-{len(criteria) + 1}", text=match.group(1)))
    return criteria


def build_tracker_objective(
    external: ExternalTask,
    *,
    connection_id: uuid.UUID,
    repository: str | None,
    actor: str,
    sender_login: str | None = None,
    delivery_id: str | None = None,
    payload_hash: str | None = None,
    task_id: uuid.UUID | None = None,
) -> tuple[AgentObjective, dict]:
    """Objective + forge-set server context for one re-fetched tracker item.

    The objective's own ``context`` is empty: provenance (``source`` label per
    ADR-0014, ``actor``, and the ``tracker`` block the write-back keys on) is
    returned separately so :func:`create_and_enqueue_run` applies it after
    stripping — tracker text has no path into it.
    """
    body = (external.description_md or "")[:MAX_TRACKER_BODY_CHARS]
    provider = external.provider.value
    ref = f"{provider}:{external.external_key}"
    objective = AgentObjective(
        task_id=task_id,
        key=external.external_key,
        objective=external.title,
        description=body,
        repo_targets=[RepoTarget(repo=repository)] if repository else [],
        acceptance_criteria=parse_acceptance_criteria(body),
    )
    server_context = {
        "source": ref,
        "actor": actor,
        "tracker": {
            "ref": ref,
            "provider": provider,
            "external_id": external.external_id,
            "external_key": external.external_key,
            "connection_id": str(connection_id),
            "sender_login": sender_login,
            "actor": actor,
            "delivery_id": delivery_id,
            # The signed body's hash: the dispatch guard dedupes on it too,
            # so a re-driven delivery with an identical body cannot dispatch.
            "payload_hash": payload_hash,
        },
    }
    return objective, server_context


# --------------------------------------------------------------------------- #
# Tracker reference resolution                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedTrackerRef:
    connection: PMConnection
    external_id: str
    #: The linked board task, when resolution went through the link store.
    task_id: uuid.UUID | None = None


_GITHUB_KEY = re.compile(r"^(?P<repo>[^#]+)#(?P<number>[1-9][0-9]*)$")


def resolve_tracker_ref(
    session: Session, workspace_id: uuid.UUID, tracker_ref: str
) -> ResolvedTrackerRef:
    """Resolve ``provider:key`` in ``workspace_id`` or raise ``TrackerRefNotFound``.

    Resolution order: the durable PM link store (``pm_task_link.external_key``),
    then — for GitHub Issues, whose keys embed the repository — the workspace
    connection for that repository. A foreign workspace's link or connection
    is indistinguishable from an unknown one.
    """
    provider_name, sep, key = tracker_ref.partition(":")
    if not sep or not provider_name or not key:
        raise TrackerRefNotFound(tracker_ref)
    try:
        provider = DbPMProvider(provider_name)
    except ValueError:
        raise TrackerRefNotFound(tracker_ref) from None

    link = session.scalar(
        select(PMTaskLink).where(
            PMTaskLink.workspace_id == workspace_id,
            PMTaskLink.provider == provider,
            PMTaskLink.external_key == key,
        )
    )
    if link is not None:
        connection = session.get(PMConnection, link.connection_id)
        if connection is None or connection.workspace_id != workspace_id:
            raise TrackerRefNotFound(tracker_ref)
        return ResolvedTrackerRef(
            connection=connection, external_id=link.external_id, task_id=link.forge_task_id
        )

    if provider is DbPMProvider.GITHUB_ISSUES:
        match = _GITHUB_KEY.match(key)
        if match is not None:
            # The stored key may be URL-form; compare normalised owner/repo.
            candidates = (
                session.execute(
                    select(PMConnection).where(
                        PMConnection.workspace_id == workspace_id,
                        PMConnection.provider == provider,
                    )
                )
                .scalars()
                .all()
            )
            connection = next(
                (
                    candidate
                    for candidate in candidates
                    if GitHubClient.owner_repo(candidate.external_project_key).lower()
                    == match["repo"].lower()
                ),
                None,
            )
            if connection is not None:
                return ResolvedTrackerRef(connection=connection, external_id=match["number"])
    raise TrackerRefNotFound(tracker_ref)


__all__ = [
    "MAX_TRACKER_BODY_CHARS",
    "SERVER_OWNED_CONTEXT",
    "TRACKER_CONTEXT_KEYS",
    "AgentEnqueueFailed",
    "DurableEngineUnavailable",
    "GovernedRunRefused",
    "RepoNotConnected",
    "ResolvedTrackerRef",
    "TaskNotFound",
    "TrackerRefNotFound",
    "UngovernedRunRefused",
    "build_tracker_objective",
    "claim_task_repository",
    "create_and_enqueue_run",
    "parse_acceptance_criteria",
    "refuse_ungoverned",
    "repo_connected",
    "resolve_tracker_ref",
    "start_governed_run",
    "ungoverned_runs_allowed",
]
