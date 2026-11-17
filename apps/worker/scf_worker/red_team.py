"""The red-team adversary on a governed run's change (#283).

After the declared checks and the independent review pass, and before the pull
request is opened, an adversary on a model family other than the builder's
attacks the diff: it authors a test, which runs in a sandbox on a fresh worktree
of the head commit (``scf_coordinator.red_team.run_red_team``). The worktree is
removed afterwards, so the test never reaches the run branch.

An adversary that did not attack is recorded ``not_run`` with the reason, never
``survived`` (#174, FR-017).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from scf_agent.sandbox import build_sandbox_provider
from scf_agent.sandbox.settings import SandboxSettings
from scf_api.observability.redaction import redact_text
from scf_contracts import ModelClient, SandboxProvider
from scf_contracts.orchestration_config import AgentRole
from scf_coordinator.red_team import HomogeneousAdversaryError, RedTeamError, run_red_team
from scf_db.role_config import SqlRoleConfigStore
from scf_orchestration_policy.role_config import resolve_effective_config
from scf_worker.review import _TIERS, ReviewerRefused
from scf_worker.run_workspace import _git_out, detached_worktree, file_at
from scf_workflow.red_team_gate import (
    REDTEAM_BLOCKED,
    REDTEAM_NOT_RUN,
    REDTEAM_SURVIVED,
    RedTeamResult,
)

__all__ = [
    "ADVERSARY_OFF",
    "RED_TEAM_CHECK",
    "RED_TEAM_OFF",
    "adversary_model",
    "attack_change",
]

#: The check id the red-team verdict is reported under, beside the declared checks.
RED_TEAM_CHECK = "red_team"
#: The adversary role's model when the workspace turned the scan off (FR-017).
ADVERSARY_OFF = "off"
#: The ``not_run`` reason of a scan the workspace turned off; the run goes on.
RED_TEAM_OFF = "red_team_off"


def adversary_model(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> str | None:
    """The adversary's model id (or ``off``) from the per-role configuration.

    ``None`` when no model is configured: the built-in default and a configured
    tier name no model family, as for the reviewer (#173).
    """
    config = resolve_effective_config(
        SqlRoleConfigStore(session), workspace_id, AgentRole.ADVERSARY, project_id=project_id
    )
    if config.source == "default" or config.model_or_tier in _TIERS:
        return None
    return config.model_or_tier


def _not_run(reason: str, detail: str = "", *, builder_model: str) -> RedTeamResult:
    evidence = {"reason": reason, **({"detail": detail} if detail else {})}
    return RedTeamResult(
        verdict=REDTEAM_NOT_RUN, kind="not_run", evidence=evidence, coder_model=builder_model
    )


def attack_change(
    repo_path: Path,
    *,
    base: str,
    head: str,
    criteria: list[dict[str, Any]],
    builder_model: str,
    adversary: str | None,
    client: Callable[[str], ModelClient],
) -> RedTeamResult:
    """Attack ``base...head`` with ``adversary``: ``blocked``, ``survived`` or ``not_run``.

    The adversary's repository instructions are the base branch's AGENTS.md: one the
    change adds is part of what it attacks, so only data (ADR-0014).
    """
    if adversary is None:
        return _not_run("red_team_not_configured", builder_model=builder_model)
    if adversary == ADVERSARY_OFF:
        return _not_run(RED_TEAM_OFF, builder_model=builder_model)
    try:
        model_client = client(adversary)
    except ReviewerRefused as exc:  # ``reviewer_client``: no key for the adversary's provider
        return _not_run("red_team_not_configured", str(exc), builder_model=builder_model)
    diff = _git_out(repo_path, "diff", "--no-color", f"{base}...{head}")
    try:
        with detached_worktree(repo_path, head) as worktree:
            found = asyncio.run(
                run_red_team(
                    diff=diff,
                    spec={"acceptance_criteria": criteria},
                    adversary_client=model_client,
                    # The provider union widens ``create``'s result; every provider is one.
                    sandbox_provider=cast(
                        SandboxProvider, build_sandbox_provider(SandboxSettings.from_env())
                    ),
                    coder_model=builder_model,
                    worktree_path=str(worktree),
                    adversary_model=adversary,
                    agents_md=file_at(repo_path, base, "AGENTS.md"),
                )
            )
    except HomogeneousAdversaryError as exc:
        return _not_run("red_team_same_family", str(exc), builder_model=builder_model)
    except RedTeamError as exc:  # an unknown family, or a run that did not finish
        return _not_run("red_team_failed", str(exc), builder_model=builder_model)
    evidence: dict[str, Any] = {"output": redact_text(found.evidence)}
    if found.failing_test_ref is not None:
        evidence["failing_test"] = asdict(found.failing_test_ref)
    if found.violation is not None:
        evidence["violation"] = asdict(found.violation)
    return RedTeamResult(
        verdict=REDTEAM_BLOCKED if found.blocked else REDTEAM_SURVIVED,
        kind=found.kind,
        evidence=evidence,
        adversary_model=found.adversary_model,
        coder_model=found.coder_model,
    )
