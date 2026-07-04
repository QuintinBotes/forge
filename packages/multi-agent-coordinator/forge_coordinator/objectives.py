"""Build the scoped, isolated :class:`AgentObjective` for one subagent (F27 §4).

The isolation contract: a subagent's objective carries **only** its assignment's
``allowed_actions`` (resolved = role ∩ task ∩ skill, never widened) and **only**
its assignment's ``context_refs`` (predecessor *artifacts* normalized into chunks)
— never another subagent's ``AgentState``, steps, or checkpoint. The child also
cannot spawn its own subagents.
"""

from __future__ import annotations

from forge_contracts import (
    AgentObjective,
    ExecutionMode,
    RepoTarget,
    SubAgentAssignment,
    SubAgentPolicy,
)

__all__ = ["build_subagent_objective"]


def build_subagent_objective(
    *,
    parent: AgentObjective,
    assignment: SubAgentAssignment,
    repo: str | None,
    integration_branch: str,
    worktree_path: str | None,
    branch_name: str | None,
) -> AgentObjective:
    """Return a fresh, role-scoped objective for ``assignment``.

    ``context["initial_context"]`` is the ISOLATED scoped context (this
    assignment's ``context_refs`` only). ``context["worktree_path"]`` /
    ``["branch_name"]`` point a code-producing subagent at its private worktree.
    """
    context: dict[str, object] = {
        "role": assignment.role.value,
        "assignment_id": assignment.id,
        "initial_context": [c.model_dump(mode="json") for c in assignment.context_refs],
        "context_refs": [c.model_dump(mode="json") for c in assignment.context_refs],
    }
    agents_md = parent.context.get("agents_md")
    if isinstance(agents_md, str):
        context["agents_md"] = agents_md
    if worktree_path is not None:
        context["worktree_path"] = worktree_path
    if branch_name is not None:
        context["branch_name"] = branch_name
    if integration_branch:
        context["integration_branch"] = integration_branch

    repo_targets: list[RepoTarget] = []
    if repo is not None:
        repo_targets = [
            RepoTarget(
                repo=repo,
                base_branch=integration_branch or "main",
                branch_prefix=f"forge/{assignment.id}",
                worktree=False,  # the coordinator manages the worktree
            )
        ]

    return AgentObjective(
        task_id=parent.task_id,
        key=parent.key,
        objective=assignment.objective,
        description=parent.description,
        instructions=parent.instructions,
        execution_mode=ExecutionMode.SINGLE_AGENT,  # the child is a single agent
        skill_profile=parent.skill_profile,
        repo_targets=repo_targets,
        knowledge_scope=parent.knowledge_scope,
        acceptance_criteria=list(assignment.acceptance_criteria),
        allowed_actions=list(assignment.allowed_actions),
        restricted_actions=list(parent.restricted_actions),
        requires_approval=parent.requires_approval,
        # A subagent can never spawn further subagents (no recursive fan-out).
        subagent_policy=SubAgentPolicy(allowed=False, allowed_roles=[], max_parallel=0),
        handoff_rules=parent.handoff_rules,
        confidence_threshold=parent.confidence_threshold,
        model=parent.model,
        context=context,
    )
