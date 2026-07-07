"""F29 — every shipped conditional example loads and its test suite runs green (AC20)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_policy import load_policy, load_test_suite, run_policy_tests
from forge_policy.tests_runner import suite_path_for

# repo_root/examples/policies/conditional
EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "policies" / "conditional"
EXAMPLE_POLICIES = sorted(
    p for p in EXAMPLES_DIR.glob("*.yaml") if not p.name.endswith(".tests.yaml")
)


def test_examples_dir_is_populated() -> None:
    assert len(EXAMPLE_POLICIES) >= 4, f"expected >=4 conditional examples in {EXAMPLES_DIR}"


@pytest.mark.parametrize("policy_path", EXAMPLE_POLICIES, ids=lambda p: p.name)
def test_example_policy_loads_and_tests_pass(policy_path: Path) -> None:
    policy = load_policy(policy_path)
    assert policy.schema_version >= 2

    suite_path = suite_path_for(policy_path)
    assert suite_path.is_file(), f"missing test suite for {policy_path.name}"
    report = run_policy_tests(policy, load_test_suite(suite_path))
    assert report.ok, f"{policy_path.name}: {report.failures}"
