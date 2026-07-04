"""Repo policy loading and permission evaluation for Forge.

Public surface (implements the frozen ``PolicyEvaluator`` contract):

* :func:`load_policy` / :class:`RepoPolicyEvaluator.load` — parse
  ``.forge/policy.yaml`` into a :class:`~forge_contracts.Policy`.
* :func:`evaluate` / :class:`RepoPolicyEvaluator.evaluate` — the flat F04
  decision for a :class:`~forge_contracts.ToolCall`.
* :class:`ConditionalPolicyEvaluator` — F29's conditional layer composed on top
  of the flat F04 base under a fail-closed precedence ladder, driven by a
  runtime-supplied :class:`PolicyContext`.
* :func:`run_policy_tests` — run a ``.forge/policy.tests.yaml`` assertion suite.
"""

from __future__ import annotations

from forge_policy.conditional import ConditionalPolicyEvaluator
from forge_policy.context import (
    POLICY_CONDITION_FIELDS,
    PolicyContext,
    build_context_from_run,
)
from forge_policy.errors import PolicyRuleError
from forge_policy.evaluator import (
    MERGE_ACTIONS,
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
from forge_policy.tests_runner import (
    PolicyTestCase,
    PolicyTestReport,
    PolicyTestSuite,
    load_test_suite,
    run_policy_tests,
    suite_path_for,
)

__version__ = "0.1.0"

__all__ = [
    "MERGE_ACTIONS",
    "POLICY_CONDITION_FIELDS",
    "POLICY_RELATIVE_PATH",
    "WRITE_ACTIONS",
    "ConditionalPolicyEvaluator",
    "PolicyContext",
    "PolicyLoadError",
    "PolicyRuleError",
    "PolicyTestCase",
    "PolicyTestReport",
    "PolicyTestSuite",
    "RepoPolicyEvaluator",
    "RepoScopedPolicyEvaluator",
    "build_context_from_run",
    "evaluate",
    "load_policies",
    "load_policy",
    "load_test_suite",
    "repo_of",
    "resolve_policy_path",
    "run_policy_tests",
    "suite_path_for",
]
