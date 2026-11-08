"""PR gates carry the repository's review rules (spec K §4.11).

The rules come from ``.forge/policy.yaml`` at the default branch's HEAD of the
workspace's repository mirror, never the run's branch, so an agent cannot
loosen its own gate. They are stamped in the shape the approval authorizer
reads (``gate_payload["review_rules"]``), which can only add requirements: the
quorum is ``max(1, min_approvals)`` and ``ApprovalService.create`` keeps the
larger of that and the default.

Fail closed (ADR-0007): only an absent or invalid policy stamps
``{"review_rules_source": "default"}`` (the task names no repository, HEAD has
no policy file, or the file does not parse or validate). Anything else (a
database error, a git failure or timeout, a repository whose mirror is missing
on this worker) raises, so ``forge.create_approval`` retries.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts import AgentObjective
from forge_db.models import Task
from forge_policy.loader import POLICY_RELATIVE_PATH, load_policy
from forge_worker.dispatch import split_repo_name, workspace_mirrors

logger = logging.getLogger(__name__)

DEFAULT_RULES_STAMP: dict[str, Any] = {"review_rules_source": "default"}


def _mirror_for(
    factory: sessionmaker[Session], workspace_id: uuid.UUID, task_id: uuid.UUID
) -> Path | None:
    """The mirror of the task's primary repository; ``None`` when it names none."""
    with factory() as session:
        task = session.get(Task, task_id)
        if task is None or task.workspace_id != workspace_id:
            raise LookupError(f"no task {task_id} in workspace {workspace_id}")
        targets = task.repo_targets
    target = AgentObjective(objective="review rules", repo_targets=targets).primary_repo_target
    if target is None:
        return None
    owner, name = split_repo_name(target.repo)
    mirror = workspace_mirrors(workspace_id) / owner / name
    if not mirror.is_dir():
        raise FileNotFoundError(f"no mirror of {target.repo} on this worker at {mirror}")
    return mirror


def _git(mirror: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(mirror), *args], check=True, capture_output=True, timeout=10
    ).stdout


def _default_branch_policy(mirror: Path) -> bytes | None:
    """The policy file at the default branch's HEAD, undecoded; ``None`` when HEAD has none."""
    # A bare mirror's HEAD is the remote's default branch; run branches never move it.
    path = POLICY_RELATIVE_PATH.as_posix()
    if not _git(mirror, "ls-tree", "--name-only", "HEAD", "--", path).strip():
        return None
    return _git(mirror, "show", f"HEAD:{path}")


def review_rules_stamp(
    factory: sessionmaker[Session], *, workspace_id: uuid.UUID, task_id: uuid.UUID
) -> dict[str, Any]:
    """The ``gate_payload`` keys a ``pr`` gate for ``task_id`` carries."""
    mirror = _mirror_for(factory, workspace_id, task_id)
    raw = _default_branch_policy(mirror) if mirror is not None else None
    if raw is None:
        logger.info("task %s has no repo policy; the pr gate keeps the default rules", task_id)
        return dict(DEFAULT_RULES_STAMP)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.yaml"
        path.write_bytes(raw)
        try:
            policy = load_policy(path)
        # pydantic's ValidationError and an undecodable file's UnicodeDecodeError are ValueErrors
        except (ValueError, yaml.YAMLError):
            logger.warning(
                "invalid repo policy for task %s; the pr gate keeps the default rules",
                task_id,
                exc_info=True,
            )
            return dict(DEFAULT_RULES_STAMP)
    return {
        "review_rules": policy.review_rules.model_dump(mode="json"),
        "review_rules_source": "repo",
    }


__all__ = ["DEFAULT_RULES_STAMP", "review_rules_stamp"]
