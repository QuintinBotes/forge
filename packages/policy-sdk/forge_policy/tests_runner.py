"""Policy-as-code testing (F29) — ``.forge/policy.tests.yaml``.

A repo ships ``{context, tool_call, expect}`` assertions next to its policy; the
maintainer runs ``forge policy test`` (exit 0/1) so policy changes are TDD-gated
in the repo's own CI, and the same suite powers ``POST /policy/repos/{id}/test``.

Foundation deviation: the slice's ``expect_effect`` is ``allow|deny``. The real
``Decision`` effect is a three-valued enum (``allow|deny|requires_approval``), so
here ``expect_effect: deny`` means "blocked" (the call may not proceed —
``Decision.allowed is False``, covering both a hard deny and a require-approval
gate); use ``expect_requires_approval`` to distinguish a gate. See the slice notes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from forge_contracts import Decision, Policy, ToolCall
from forge_policy.conditional import ConditionalPolicyEvaluator
from forge_policy.context import PolicyContext


class PolicyTestCase(BaseModel):
    """One policy-as-code assertion."""

    name: str
    context: PolicyContext = Field(default_factory=PolicyContext.empty)
    tool_call: ToolCall
    expect_effect: Literal["allow", "deny", "requires_approval"]
    expect_requires_approval: bool | None = None
    expect_rule: str | None = None


class PolicyTestSuite(BaseModel):
    """A loaded ``.forge/policy.tests.yaml`` file."""

    cases: list[PolicyTestCase] = Field(default_factory=list)


class PolicyTestReport(BaseModel):
    """The outcome of running a :class:`PolicyTestSuite` against a policy."""

    total: int
    passed: int
    failed: int
    failures: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def _check(case: PolicyTestCase, decision: Decision) -> str | None:
    """Return a human description of the mismatch, or ``None`` when it passes."""
    if case.expect_effect == "allow" and not decision.allowed:
        return f"expected allow, got {decision.effect.value}"
    if case.expect_effect == "deny" and decision.allowed:
        return f"expected deny (blocked), got {decision.effect.value}"
    if case.expect_effect == "requires_approval" and not decision.requires_approval:
        return f"expected requires_approval, got {decision.effect.value}"
    if (
        case.expect_requires_approval is not None
        and decision.requires_approval is not case.expect_requires_approval
    ):
        return (
            f"expected requires_approval={case.expect_requires_approval}, "
            f"got {decision.requires_approval}"
        )
    if case.expect_rule is not None and case.expect_rule not in {
        m.rule_id for m in decision.conditional_matches
    }:
        return f"expected rule {case.expect_rule!r} in conditional_matches"
    return None


def run_policy_tests(policy: Policy, suite: PolicyTestSuite) -> PolicyTestReport:
    """Run every case in ``suite`` against ``policy`` and return a report."""
    evaluator = ConditionalPolicyEvaluator()
    failures: list[dict[str, Any]] = []
    passed = 0
    for case in suite.cases:
        decision = evaluator.evaluate_in_context(case.tool_call, policy, case.context)
        mismatch = _check(case, decision)
        if mismatch is None:
            passed += 1
        else:
            failures.append(
                {
                    "name": case.name,
                    "expected": {
                        "effect": case.expect_effect,
                        "requires_approval": case.expect_requires_approval,
                        "rule": case.expect_rule,
                    },
                    "actual": {
                        "effect": decision.effect.value,
                        "requires_approval": decision.requires_approval,
                        "matched_rule": decision.matched_rule,
                        "reason": mismatch,
                    },
                }
            )
    return PolicyTestReport(
        total=len(suite.cases), passed=passed, failed=len(failures), failures=failures
    )


def load_test_suite(path: str | Path) -> PolicyTestSuite:
    """Load + validate a ``.tests.yaml`` policy test suite."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if raw is None:
        return PolicyTestSuite(cases=[])
    if not isinstance(raw, dict):
        raise ValueError(f"policy test file must be a YAML mapping: {path}")
    return PolicyTestSuite.model_validate(raw)


def suite_path_for(policy_path: str | Path) -> Path:
    """The conventional ``.tests.yaml`` path sitting next to a policy file.

    ``foo.yaml`` -> ``foo.tests.yaml``; ``.forge/policy.yaml`` ->
    ``.forge/policy.tests.yaml``.
    """
    p = Path(policy_path)
    return p.with_name(f"{p.stem}.tests{p.suffix}")


__all__ = [
    "PolicyTestCase",
    "PolicyTestReport",
    "PolicyTestSuite",
    "load_test_suite",
    "run_policy_tests",
    "suite_path_for",
]
