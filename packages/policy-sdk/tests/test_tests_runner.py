"""F29 — the policy-as-code test runner (AC19)."""

from __future__ import annotations

from pathlib import Path

from forge_contracts import Policy
from forge_policy import load_policy, load_test_suite, run_policy_tests
from forge_policy.tests_runner import PolicyTestSuite, suite_path_for

FIXTURES = Path(__file__).parent / "fixtures"
CANONICAL = FIXTURES / "policy_conditional_canonical.yaml"


def test_suite_passes() -> None:
    policy = load_policy(CANONICAL)
    suite = load_test_suite(suite_path_for(CANONICAL))
    report = run_policy_tests(policy, suite)
    assert report.ok is True
    assert report.failed == 0
    assert report.passed == report.total > 0


def test_suite_reports_failure() -> None:
    policy = load_policy(CANONICAL)
    suite = load_test_suite(suite_path_for(CANONICAL))
    # Flip one expectation so it must fail.
    suite.cases[2].expect_effect = "allow"  # infra write on feature branch is actually a deny
    report = run_policy_tests(policy, suite)
    assert report.ok is False
    assert report.failed == 1
    failure = report.failures[0]
    assert failure["name"] == suite.cases[2].name
    assert "expected" in failure and "actual" in failure


def test_suite_path_for_naming() -> None:
    assert suite_path_for("foo.yaml").name == "foo.tests.yaml"
    assert suite_path_for(Path(".forge/policy.yaml")).name == "policy.tests.yaml"


def test_empty_suite_is_ok() -> None:
    report = run_policy_tests(Policy(repo_id="r"), PolicyTestSuite())
    assert report.ok is True
    assert report.total == 0
