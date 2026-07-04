"""Merge-plan builder + cross-repo dependency cycle detection (F22).

``MergePlanBuilder.build`` validates that a multi-repo task names exactly one
``primary`` repo, builds the ``depends_on`` DAG, refuses cycles and dangling
dependency edges, and returns a topologically-sorted :class:`MergePlan` whose
``merge_order`` lists dependency roots first (the ``primary`` sorts first among
repos with no outstanding dependency). The single-repo case (the V1 shape) is
accepted verbatim: the lone target is the primary regardless of its role.

Pure logic — no I/O, no DB — so the plan is fully reproducible for the audit log.
"""

from __future__ import annotations

from forge_contracts import CycleError, MergePlan, RepoTarget
from forge_workflow.exceptions import WorkflowError


class MultipleOrNoPrimaryError(WorkflowError):
    """Raised when a multi-repo task does not have exactly one ``role=primary``."""

    def __init__(self, primaries: list[str]) -> None:
        self.primaries = primaries
        if primaries:
            msg = f"expected exactly one primary repo, found {len(primaries)}: {primaries!r}"
        else:
            msg = "expected exactly one primary repo, found none (set role='primary')"
        super().__init__(msg)


class CyclicRepoDependencyError(WorkflowError, CycleError):
    """Raised when the ``depends_on`` edges form a cycle (refused before run)."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"cyclic repo dependency: {' -> '.join(cycle)}")


class UnknownDependencyRepoError(WorkflowError):
    """Raised when a ``depends_on`` names a repo that is not a task target."""

    def __init__(self, repo: str, missing: str) -> None:
        self.repo = repo
        self.missing = missing
        super().__init__(f"repo {repo!r} depends_on unknown repo {missing!r}")


def _find_cycle(deps_map: dict[str, list[str]]) -> list[str] | None:
    """Return a cycle ``[a, b, ..., a]`` if one exists, else ``None`` (DFS)."""
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(deps_map, white)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = gray
        stack.append(node)
        for dep in deps_map[node]:
            if color[dep] == gray:
                start = stack.index(dep)
                return [*stack[start:], dep]
            if color[dep] == white:
                found = visit(dep)
                if found is not None:
                    return found
        stack.pop()
        color[node] = black
        return None

    for node in deps_map:
        if color[node] == white:
            found = visit(node)
            if found is not None:
                return found
    return None


class MergePlanBuilder:
    """Build a dependency-ordered :class:`MergePlan` from a task's repo targets."""

    @staticmethod
    def build(repo_targets: list[RepoTarget]) -> MergePlan:
        if not repo_targets:
            raise WorkflowError("cannot build a merge plan for zero repo targets")

        repo_ids = [t.repo for t in repo_targets]

        # --- resolve the primary -------------------------------------------- #
        if len(repo_targets) == 1:
            primary = repo_targets[0].repo
        else:
            primaries = [t.repo for t in repo_targets if t.role == "primary"]
            if len(primaries) != 1:
                raise MultipleOrNoPrimaryError(primaries)
            primary = primaries[0]

        # --- build + validate the dependency edges -------------------------- #
        known = set(repo_ids)
        deps_map: dict[str, list[str]] = {}
        for target in repo_targets:
            for dep in target.depends_on:
                if dep not in known:
                    raise UnknownDependencyRepoError(target.repo, dep)
            # Preserve declared order, drop self-edges / duplicates.
            seen: list[str] = []
            for dep in target.depends_on:
                if dep != target.repo and dep not in seen:
                    seen.append(dep)
            deps_map[target.repo] = seen

        # --- refuse cycles before any worktree is created ------------------- #
        cycle = _find_cycle(deps_map)
        if cycle is not None:
            raise CyclicRepoDependencyError(cycle)

        # --- topological order (deps first; primary first among equals) ----- #
        merge_order: list[str] = []
        emitted: set[str] = set()
        index = {repo: i for i, repo in enumerate(repo_ids)}
        remaining = list(repo_ids)
        while remaining:
            available = [r for r in remaining if all(d in emitted for d in deps_map[r])]
            # `cycle` is None here, so at least one node is always available.
            available.sort(key=lambda r: (r != primary, index[r]))
            chosen = available[0]
            merge_order.append(chosen)
            emitted.add(chosen)
            remaining.remove(chosen)

        return MergePlan(primary_repo_id=primary, merge_order=merge_order, edges=deps_map)


__all__ = [
    "CyclicRepoDependencyError",
    "MergePlanBuilder",
    "MultipleOrNoPrimaryError",
    "UnknownDependencyRepoError",
]
