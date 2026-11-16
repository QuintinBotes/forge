"""The independent reviewer of a governed run's change (#173).

After the declared checks pass and before the pull request is opened, a reviewer
agent on a model family other than the builder's reads the diff against the
acceptance criteria, with read-only repository tools, and returns a verdict.
The diff and the criteria are untrusted data in its prompt (ADR-0014).
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from scf_agent.providers import ModelClientConfig, ModelClientError, build_model_client
from scf_agent.providers.family import UnknownModelFamily, provider_family, same_family
from scf_agent.tools import ToolRegistry, agent_tool_belt
from scf_contracts import AgentObjective, AgentRunResult, ModelClient, RunStatus
from scf_contracts.orchestration_config import AgentRole
from scf_contracts.review import ReviewVerdict
from scf_db.role_config import SqlRoleConfigStore
from scf_orchestration_policy.role_config import resolve_effective_config
from scf_worker.agent_runner import (
    _default_redactor,
    _run_token_budget,
    build_agent_runner,
    run_objective,
)
from scf_worker.run_workspace import _git_out, detached_worktree

__all__ = [
    "DIFF_CAP_CHARS",
    "READ_ONLY_TOOLS",
    "REVIEWER_ROLE",
    "HomogeneousReviewerError",
    "ReviewerRefused",
    "ensure_heterogeneous",
    "parse_verdict",
    "review_change",
    "reviewer_client",
    "reviewer_model",
    "reviewer_tools",
]

#: The ``agent_run.role`` of a reviewer run; the builder's runs keep ``primary``.
REVIEWER_ROLE = "reviewer"
#: The most of the diff the reviewer is given; a longer one is cut and says so.
DIFF_CAP_CHARS = 60_000
#: The reviewer's whole tool registry: it reads, and nothing else.
READ_ONLY_TOOLS = ("read_file", "list_dir", "search_code")
_TIERS = frozenset({"junior", "medior", "senior"})

_INSTRUCTIONS = (
    "You are the independent reviewer. Read the change against every acceptance "
    "criterion; your tools read the repository at the change's head commit. Request "
    "changes when a criterion is not met or the change has a defect; otherwise approve. "
    "Finish with output that is only this JSON object: "
    '{"verdict": "approve" | "request_changes", "findings": [{"file": "<path>", '
    '"line": <number>, "severity": "blocking" | "important" | "minor" | "nit", '
    '"statement": "<what is wrong>"}]}'
)


class ReviewerRefused(Exception):
    """No independent review can be made; ``reason`` names why, for the run's pause."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class HomogeneousReviewerError(ReviewerRefused):
    """The reviewer's model family is the builder's: it would judge its own work."""

    def __init__(self, message: str) -> None:
        super().__init__("reviewer_same_family", message)


def reviewer_model(session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """The reviewer's model id from the per-role configuration.

    The built-in default is a tier, not a model a person chose, so it counts as not
    configured; so does a configured tier, which names no model family.
    """
    config = resolve_effective_config(
        SqlRoleConfigStore(session), workspace_id, AgentRole.REVIEWER, project_id=project_id
    )
    if config.source == "default":
        raise ReviewerRefused("reviewer_not_configured", "no reviewer model is configured")
    if config.model_or_tier in _TIERS:
        raise ReviewerRefused(
            "reviewer_not_configured",
            f"the reviewer role names the tier {config.model_or_tier!r}, not a model",
        )
    return config.model_or_tier


def ensure_heterogeneous(builder_model: str | None, reviewer: str) -> None:
    """Refuse a reviewer of the builder's family before any model call."""
    if not builder_model:
        raise ReviewerRefused(
            "reviewer_family_unknown", "the builder's model is not recorded on its run"
        )
    try:
        same = same_family(builder_model, reviewer)
    except UnknownModelFamily as exc:
        raise ReviewerRefused("reviewer_family_unknown", str(exc)) from exc
    if same:
        raise HomogeneousReviewerError(
            f"reviewer {reviewer!r} is of the builder's family ({builder_model!r})"
        )


def reviewer_client(model: str) -> ModelClient:
    """A client for ``model`` on its own provider, with that provider's key."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_MODEL_")}
    family = provider_family(model)
    config = ModelClientConfig.from_env(
        {**env, "FORGE_MODEL_PROVIDER": family.value, "FORGE_MODEL_NAME": model}
    )
    if config is None:
        raise ReviewerRefused(
            "reviewer_not_configured", f"no API key for the reviewer's provider {family.value}"
        )
    try:
        return build_model_client(config, redactor=_default_redactor())
    except ModelClientError as exc:
        raise ReviewerRefused("reviewer_not_configured", str(exc)) from exc


def reviewer_tools(root: Path) -> ToolRegistry:
    """Read-only tools over ``root``: no tool writes files, commits or runs commands."""
    belt = agent_tool_belt(root, run_id="independent-review")
    registry = ToolRegistry()
    for name in READ_ONLY_TOOLS:
        tool = belt.get(name)
        assert tool is not None, name  # the belt always carries the read tools
        registry.register(tool)
    return registry


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_verdict(output: str | None) -> ReviewVerdict:
    """The reviewer's verdict, or ``request_changes`` when its output does not parse."""
    text = (output or "").strip()
    fenced = _FENCE.fullmatch(text)
    try:
        verdict = ReviewVerdict.model_validate_json(fenced.group(1) if fenced else text)
    except ValueError:
        return ReviewVerdict(verdict="request_changes", reason="unparseable_verdict")
    # Only the reviewer's judgement comes from its output; the rest is the station's.
    return verdict.model_copy(
        update={"reason": None, "truncated": False, "reviewer_model": None, "builder_model": None}
    )


def review_change(
    repo_path: Path,
    *,
    base: str,
    head: str,
    criteria: list[str],
    client: ModelClient,
    reviewer: str,
    builder_model: str,
) -> tuple[ReviewVerdict, AgentObjective, AgentRunResult]:
    """Run the reviewer on ``base...head``: its verdict, its objective and how it ended.

    Only a reviewer run that succeeded can approve; one that failed or escalated
    requests changes (``reviewer_run_failed``).
    """
    diff = _git_out(repo_path, "diff", "--no-color", f"{base}...{head}")
    truncated = len(diff) > DIFF_CAP_CHARS
    objective = AgentObjective(
        objective="Review the change below against its acceptance criteria.",
        instructions=_INSTRUCTIONS,
        allowed_actions=["read_repo"],
        context={
            "review": {"criteria": criteria, "diff": diff[:DIFF_CAP_CHARS], "truncated": truncated},
            # The change may add or edit AGENTS.md files: only the base branch's are
            # instructions, and the runtime would otherwise read the head's.
            "agents_md": _base_agents_md(repo_path, base),
        },
    )
    with detached_worktree(repo_path, head) as worktree:
        runner = build_agent_runner(
            model_client=client,
            tools=reviewer_tools(worktree),
            repo_root=worktree,
            token_budget=_run_token_budget(),
        )
        result = run_objective(runner, objective)
    verdict = (
        parse_verdict(result.output)
        if result.status == RunStatus.SUCCEEDED
        else ReviewVerdict(verdict="request_changes", reason="reviewer_run_failed")
    )
    station = {"truncated": truncated, "reviewer_model": reviewer, "builder_model": builder_model}
    return verdict.model_copy(update=station), objective, result


def _base_agents_md(repo_path: Path, base: str) -> str:
    """The base branch's root AGENTS.md, or nothing when it has none."""
    try:
        return _git_out(repo_path, "show", f"{base}:AGENTS.md")
    except subprocess.CalledProcessError:
        return ""
