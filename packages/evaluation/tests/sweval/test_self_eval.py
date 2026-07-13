"""Offline tests for the Self-Eval run aggregation over a private suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_agent.sandbox import LocalSandboxProvider
from forge_eval.golden import GoldenCase
from forge_eval.sweval import run_self_eval

_BROKEN = "def add(a, b):\n    return a - b\n"
_FIXED = "def add(a, b):\n    return a + b\n"
_TEST = "from mymod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_FTP = "test_mymod.py::test_add"


def _make_worktree(root: Path, name: str) -> Path:
    wt = root / name
    wt.mkdir()
    (wt / "mymod.py").write_text(_BROKEN, encoding="utf-8")
    (wt / "test_mymod.py").write_text(_TEST, encoding="utf-8")
    return wt


def _case(cid: str) -> GoldenCase:
    return GoldenCase(
        id=cid,
        query="make add correct",
        expected_ids=[_FTP],
        kind="agent_task",
        metadata={"fail_to_pass": [_FTP], "pass_to_pass": []},
    )


@pytest.mark.asyncio
async def test_all_resolved_gives_full_rate(tmp_path: Path) -> None:
    cases = [_case("a"), _case("b")]
    worktrees = {c.id: _make_worktree(tmp_path, c.id) for c in cases}
    card = await run_self_eval(
        cases=cases,
        solve_fn=lambda _c: {"mymod.py": _FIXED},
        sandbox_provider=LocalSandboxProvider(),
        worktree_for=lambda c: str(worktrees[c.id]),
    )
    assert card.total == 2
    assert card.resolved == 2
    assert card.resolution_rate == 1.0
    assert card.meets(1.0) is True


@pytest.mark.asyncio
async def test_regression_lowers_rate_and_fails_baseline(tmp_path: Path) -> None:
    cases = [_case("a"), _case("b")]
    worktrees = {c.id: _make_worktree(tmp_path, c.id) for c in cases}

    # A config that only solves case "a" (leaves "b" broken). case.id survives
    # redaction (only the hidden-test metadata is stripped).
    def solve(case: GoldenCase) -> dict[str, str]:
        return {"mymod.py": _FIXED} if case.id == "a" else {"mymod.py": _BROKEN}

    card = await run_self_eval(
        cases=cases,
        solve_fn=solve,
        sandbox_provider=LocalSandboxProvider(),
        worktree_for=lambda c: str(worktrees[c.id]),
    )
    assert card.resolved == 1
    assert card.resolution_rate == 0.5
    assert card.meets(1.0) is False  # regressed vs a 100% baseline
    assert card.meets(0.5) is True
