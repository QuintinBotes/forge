"""Repo policy loading and permission evaluation for Forge.

Public surface (implements the frozen ``PolicyEvaluator`` contract):

* :func:`load_policy` / :class:`RepoPolicyEvaluator.load` — parse
  ``.forge/policy.yaml`` into a :class:`~forge_contracts.Policy`.
* :func:`evaluate` / :class:`RepoPolicyEvaluator.evaluate` — decide whether a
  :class:`~forge_contracts.ToolCall` is permitted, returning a
  :class:`~forge_contracts.Decision`.
"""

from __future__ import annotations

from forge_policy.evaluator import (
    WRITE_ACTIONS,
    RepoPolicyEvaluator,
    RepoScopedPolicyEvaluator,
    evaluate,
    repo_of,
)
from forge_policy.loader import (
    POLICY_RELATIVE_PATH,
    PolicyLoadError,
    load_policies,
    load_policy,
    resolve_policy_path,
)

__version__ = "0.1.0"

__all__ = [
    "POLICY_RELATIVE_PATH",
    "WRITE_ACTIONS",
    "PolicyLoadError",
    "RepoPolicyEvaluator",
    "RepoScopedPolicyEvaluator",
    "evaluate",
    "load_policies",
    "load_policy",
    "repo_of",
    "resolve_policy_path",
]
