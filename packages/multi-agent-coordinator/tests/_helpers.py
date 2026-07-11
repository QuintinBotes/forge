"""Hermetic test helpers for the F27 coordinator suite.

Importable as a plain module (``from _helpers import ...``) — pytest's prepend
import mode puts this directory on ``sys.path``. No network, no live LLM.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from forge_contracts import (
    AcceptanceCriterion,
    AgentObjective,
    AgentRunResult,
    RepoChangeSet,
    RepoTarget,
    RunStatus,
    SubAgentPolicy,
    SubagentRules,
)

__all__ = [
    "AgentScript",
    "CallRecord",
    "ScriptedExecutionAgent",
    "ScriptingHub",
    "make_objective",
    "obj_parent",
]


@dataclass
class AgentScript:
    """A canned outcome for one subagent run."""

    confidence: float = 0.9
    files: list[tuple[str, str]] = field(default_factory=list)
    review_verdict: str | None = None
    findings: list[str] = field(default_factory=list)
    needs_human: bool = False
    status: str = "succeeded"  # succeeded | failed
    summary: str = "done"
    token_usage: dict[str, int] = field(default_factory=lambda: {"input": 10, "output": 5})
    sleep: float = 0.0


@dataclass
class CallRecord:
    role: str
    assignment_id: str
    allowed_actions: list[str]
    initial_context: list[dict]
    objective_text: str


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class ScriptingHub:
    """Holds the scripts + records every subagent call (for assertions)."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[AgentScript]] = {}
        self.calls: list[CallRecord] = []
        self.model_clients: list[object] = []
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._active = 0
        self.max_concurrency = 0

    def set(self, role: str, *scripts: AgentScript) -> None:
        self.scripts[role] = list(scripts)

    def script_for(self, role: str) -> AgentScript:
        with self._lock:
            attempt = self._attempts.get(role, 0)
            self._attempts[role] = attempt + 1
        seq = self.scripts.get(role) or [AgentScript()]
        return seq[min(attempt, len(seq) - 1)]

    def enter(self) -> None:
        with self._lock:
            self._active += 1
            self.max_concurrency = max(self.max_concurrency, self._active)

    def exit(self) -> None:
        with self._lock:
            self._active -= 1

    def agent_factory(self, model_client=None):  # (ModelClient | None) -> AgentRuntime
        # Records the per-role model client the coordinator built (or None), so a
        # test can prove different roles were routed to different models. The
        # scripted agent itself ignores the client.
        self.model_clients.append(model_client)
        return ScriptedExecutionAgent(self)

    def calls_for(self, role: str) -> list[CallRecord]:
        return [c for c in self.calls if c.role == role]


class ScriptedExecutionAgent:
    """An ``AgentRuntime`` fake (sync ``run``)."""

    def __init__(self, hub: ScriptingHub) -> None:
        self._hub = hub

    def run(self, objective: AgentObjective) -> AgentRunResult:
        ctx = objective.context
        role = str(ctx.get("role", "implementer"))
        assignment_id = str(ctx.get("assignment_id", ""))
        self._hub.calls.append(
            CallRecord(
                role=role,
                assignment_id=assignment_id,
                allowed_actions=list(objective.allowed_actions),
                initial_context=list(ctx.get("initial_context", [])),
                objective_text=objective.objective,
            )
        )
        script = self._hub.script_for(role)

        self._hub.enter()
        try:
            if script.sleep:
                time.sleep(script.sleep)
        finally:
            self._hub.exit()

        repo_change_sets: list[RepoChangeSet] = []
        worktree = ctx.get("worktree_path")
        branch = ctx.get("branch_name")
        if script.files and worktree:
            wt = Path(str(worktree))
            written: list[str] = []
            for rel, content in script.files:
                target = wt / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                written.append(rel)
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", f"{role}: {script.summary}")
            head = _git(wt, "rev-parse", "HEAD")
            repo_change_sets.append(
                RepoChangeSet(
                    repo=str(objective.repo_targets[0].repo) if objective.repo_targets else "",
                    branch_name=str(branch) if branch else None,
                    head_commit_sha=head,
                    changed_files=written,
                    has_changes=True,
                )
            )

        status = RunStatus.SUCCEEDED
        if script.needs_human:
            status = RunStatus.ESCALATED
        elif script.status == "failed":
            status = RunStatus.FAILED

        return AgentRunResult(
            run_id=uuid.uuid4(),
            task_id=objective.task_id,
            status=status,
            confidence=script.confidence,
            needs_human=script.needs_human,
            output=script.summary,
            summary=script.summary,
            artifacts={
                "review_verdict": script.review_verdict,
                "findings": list(script.findings),
                "token_usage": dict(script.token_usage),
                "summary": script.summary,
            },
            repo_change_sets=repo_change_sets,
        )


def make_objective(
    repo: Path | None = None,
    *,
    objective: str = "Implement feature X",
    rules: SubagentRules | None = None,
    task_policy: SubAgentPolicy | None = None,
    pattern: str | None = None,
    review_required: bool = False,
    acceptance: list[AcceptanceCriterion] | None = None,
    allowed_actions: list[str] | None = None,
    context_extra: dict | None = None,
    key: str = "TASK-123",
) -> AgentObjective:
    ctx: dict = {"workspace_id": str(uuid.uuid4()), "parent_agent_run_id": str(uuid.uuid4())}
    if rules is not None:
        ctx["subagent_rules"] = rules.model_dump()
    if pattern is not None:
        ctx["coordination_pattern"] = pattern
    if review_required:
        ctx["review_required"] = True
    if context_extra:
        ctx.update(context_extra)
    repo_targets = []
    if repo is not None:
        repo_targets = [RepoTarget(repo=str(repo), base_branch="main", worktree=False)]
    return AgentObjective(
        task_id=uuid.uuid4(),
        key=key,
        objective=objective,
        repo_targets=repo_targets,
        acceptance_criteria=acceptance or [],
        allowed_actions=allowed_actions or [],
        subagent_policy=task_policy or SubAgentPolicy(allowed=True, max_parallel=2),
        context=ctx,
    )


def obj_parent(obj: AgentObjective) -> uuid.UUID:
    return uuid.UUID(obj.context["parent_agent_run_id"])
