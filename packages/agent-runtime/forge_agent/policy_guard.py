"""Repo-aware policy guard for multi-repo agent runs (F22).

:class:`MultiRepoPolicyGuard` holds one :class:`~forge_contracts.Policy` per repo
and, for every tool call, selects the policy by ``call.repo`` before evaluating —
there is **no merged super-policy** and **no implicit default repo**. On top of
the per-repo policy decision it enforces *worktree confinement per repo*: a tool
scoped to repo A may only touch paths under A's worktree, so a ``..``/absolute
path escaping into another repo's worktree is denied (cross-repo path escape).
"""

from __future__ import annotations

from pathlib import Path

from forge_contracts import (
    Decision,
    DecisionEffect,
    Policy,
    PolicyEvaluator,
    ToolCall,
    UnknownRepoError,
)

__all__ = ["MultiRepoPolicyGuard"]

#: Path-bearing tool/action names whose target must be confined to the repo.
_PATH_ACTIONS = frozenset(
    {
        "read_repo",
        "read_file",
        "write_code",
        "write_file",
        "apply_patch",
        "edit",
        "edit_file",
        "create_file",
        "modify_file",
        "delete_file",
        "delete_files",
    }
)


def _repo_of(call: ToolCall) -> str | None:
    for source in (call.arguments, call.metadata):
        value = source.get("repo")
        if isinstance(value, str) and value:
            return value
    return None


def _target_path(call: ToolCall) -> str | None:
    if call.path:
        return call.path
    for key in ("path", "file", "filename", "target"):
        value = call.arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_confined(worktree: Path, raw: str) -> bool:
    candidate = (worktree / raw).resolve()
    root = worktree.resolve()
    return root == candidate or root in candidate.parents


class MultiRepoPolicyGuard:
    """Select a repo's policy + worktree and decide a tool call (F22)."""

    def __init__(
        self,
        policies: dict[str, Policy],
        evaluator: PolicyEvaluator,
        *,
        worktrees: dict[str, str | Path] | None = None,
    ) -> None:
        self._policies = dict(policies)
        self._evaluator = evaluator
        self._worktrees = {k: Path(v) for k, v in (worktrees or {}).items()}

    def _policy_for(self, repo: str | None) -> Policy:
        if repo is None or repo not in self._policies:
            raise UnknownRepoError(repo)
        return self._policies[repo]

    def check(self, call: ToolCall) -> Decision:
        """Decide ``call`` against ``policies[call.repo]`` + worktree confinement.

        Raises :class:`~forge_contracts.UnknownRepoError` when the call does not
        name an in-scope repo (no implicit default). When the call is path-bearing
        and worktrees are known, a path escaping the repo's worktree is denied
        even if the policy would otherwise allow it.
        """
        repo = _repo_of(call)
        policy = self._policy_for(repo)
        assert repo is not None  # _policy_for raises otherwise

        action = (call.action or call.tool or "").strip()
        path = _target_path(call)
        if action in _PATH_ACTIONS and path is not None and repo in self._worktrees:
            confinement = self.check_write_path(repo, path)
            if not confinement.allowed:
                return confinement

        return self._evaluator.evaluate(call, policy)

    def check_write_path(self, repo: str, path: str) -> Decision:
        """Confine ``path`` to ``repo``'s worktree (cross-repo escape denied)."""
        self._policy_for(repo)  # repo must be in scope
        worktree = self._worktrees.get(repo)
        if worktree is None:
            raise UnknownRepoError(repo)
        if _is_confined(worktree, path):
            return Decision(
                effect=DecisionEffect.ALLOW,
                reason=f"path {path!r} is confined to repo {repo!r} worktree",
                matched_rule="worktree_confinement",
            )
        return Decision(
            effect=DecisionEffect.DENY,
            reason=f"path {path!r} escapes repo {repo!r} worktree",
            matched_rule="worktree_confinement",
        )

    def check_command(self, repo: str, command: str) -> Decision:
        """Evaluate a command against ``repo``'s policy (repo must be in scope)."""
        policy = self._policy_for(repo)
        call = ToolCall(tool=command, action=command, arguments={"repo": repo})
        return self._evaluator.evaluate(call, policy)
