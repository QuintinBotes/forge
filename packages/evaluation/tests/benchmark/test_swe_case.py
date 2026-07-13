"""F41 unit tests — minted Self-Eval Gate case sandbox metadata."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge_eval.benchmark import (
    BenchmarkFrozenError,
    SweCaseFields,
    parse_swe_case_fields,
    validate_freezable,
)
from forge_eval.golden import GoldenCase


def _minted_case(**metadata_overrides) -> GoldenCase:
    metadata = {
        "fail_to_pass": ["tests/test_auth.py::test_refresh"],
        "pass_to_pass": ["tests/test_auth.py::test_login"],
        "sandbox_image": "forge/sandbox:py3.14",
        "setup_commands": ["uv sync"],
        "base_commit": "abc123",
        "expected_terminal_state": "pr_opened",
    }
    metadata.update(metadata_overrides)
    return GoldenCase(
        id="issue-42",
        query="Fix token refresh race",
        expected_ids=["ISSUE-42"],
        kind="agent_task",
        metadata=metadata,
    )


def test_parse_swe_case_fields_roundtrip() -> None:
    case = _minted_case()
    fields = parse_swe_case_fields(case)
    assert fields == SweCaseFields(
        fail_to_pass=["tests/test_auth.py::test_refresh"],
        pass_to_pass=["tests/test_auth.py::test_login"],
        sandbox_image="forge/sandbox:py3.14",
        setup_commands=["uv sync"],
        base_commit="abc123",
    )


def test_parse_swe_case_fields_defaults_on_empty_metadata() -> None:
    case = GoldenCase(id="x", query="q", expected_ids=["y"])
    fields = parse_swe_case_fields(case)
    assert fields.fail_to_pass == []
    assert fields.pass_to_pass == []
    assert fields.sandbox_image is None
    assert fields.setup_commands == []
    assert fields.base_commit is None


def test_parse_swe_case_fields_rejects_wrong_types() -> None:
    case = _minted_case(fail_to_pass="not-a-list")
    with pytest.raises(ValidationError):
        parse_swe_case_fields(case)


def test_minted_case_at_pr_opened_is_freezable() -> None:
    """AC24: a minted case terminating at pr_opened (not merged) may freeze."""
    validate_freezable([_minted_case()])  # does not raise


def test_minted_case_declaring_merged_is_rejected() -> None:
    """AC24: hidden tests never let a minted case terminate at merged."""
    bad = _minted_case(expected_terminal_state="merged")
    with pytest.raises(BenchmarkFrozenError, match="merged"):
        validate_freezable([bad])
