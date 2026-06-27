"""F22 multi-repo execution: merge planning, aggregate gate, ordered merger.

This subpackage widens the single-repo merge/verify arc to ``len(repo_targets) >
1`` while leaving the V1 single-repo path untouched (the worker-side dispatch
rule selects these variants only when a task has more than one repo target).

Public surface:

* :class:`MergePlanBuilder` — validate primaries, build the ``depends_on`` DAG,
  detect cycles, and topologically order the merge (``MergePlan``).
* :class:`MultiRepoMergeGate` — aggregate per-repo mergeability into one gate
  (no PR merges until *every* required, changed repo is ready).
* :class:`MultiRepoMerger` — merge in dependency order, halting on the first
  failure and recording exactly which repos merged (partial-merge audit).
"""

from __future__ import annotations

from forge_workflow.multi_repo.merge import (
    MultiRepoMergeGate,
    MultiRepoMerger,
    RepoMergeClient,
)
from forge_workflow.multi_repo.plan import (
    CyclicRepoDependencyError,
    MergePlanBuilder,
    MultipleOrNoPrimaryError,
    UnknownDependencyRepoError,
)

__all__ = [
    "CyclicRepoDependencyError",
    "MergePlanBuilder",
    "MultiRepoMergeGate",
    "MultiRepoMerger",
    "MultipleOrNoPrimaryError",
    "RepoMergeClient",
    "UnknownDependencyRepoError",
]
