"""Offline tests for the Self-Eval Gate sandboxed runner.

Uses the local ``worktree`` sandbox provider on a temp checkout — no network,
no live model. A candidate patch that fixes the module resolves the case; a
wrong patch does not; hidden tests are never handed to ``solve_fn``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_agent.sandbox import LocalSandboxProvider
from forge_eval.golden import GoldenCase
from forge_eval.sweval import run_swe_case

_BROKEN = "def add(a, b):\n    return a - b\n"
_FIXED = "def add(a, b):\n    return a + b\n"
_TEST = "from mymod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_KEEP = "def test_keep():\n    assert True\n"

_FTP = "test_mymod.py::test_add"
_PTP = "test_keep.py::test_keep"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    (tmp_path / "mymod.py").write_text(_BROKEN, encoding="utf-8")
    (tmp_path / "test_mymod.py").write_text(_TEST, encoding="utf-8")
    (tmp_path / "test_keep.py").write_text(_KEEP, encoding="utf-8")
    return tmp_path


def _case(*, fail: list[str], keep: list[str] | None = None) -> GoldenCase:
    return GoldenCase(
        id="swe-1",
        query="make add() correct",
        expected_ids=list(fail),
        kind="agent_task",
        metadata={"fail_to_pass": list(fail), "pass_to_pass": list(keep or [])},
    )


@pytest.mark.asyncio
async def test_correct_patch_resolves(worktree: Path) -> None:
    res = await run_swe_case(
        case=_case(fail=[_FTP], keep=[_PTP]),
        solve_fn=lambda _c: {"mymod.py": _FIXED},
        sandbox_provider=LocalSandboxProvider(),
        worktree_path=str(worktree),
    )
    assert res.resolved is True
    assert res.output_ids == [_FTP]
    assert res.regressed == []


@pytest.mark.asyncio
async def test_wrong_patch_does_not_resolve(worktree: Path) -> None:
    res = await run_swe_case(
        case=_case(fail=[_FTP]),
        solve_fn=lambda _c: {"mymod.py": _BROKEN},  # still wrong
        sandbox_provider=LocalSandboxProvider(),
        worktree_path=str(worktree),
    )
    assert res.resolved is False
    assert res.output_ids == []


@pytest.mark.asyncio
async def test_pass_to_pass_regression_fails_the_case(worktree: Path) -> None:
    # A patch that fixes fail-to-pass but breaks a pass-to-pass test must NOT resolve.
    res = await run_swe_case(
        case=_case(fail=[_FTP], keep=[_PTP]),
        solve_fn=lambda _c: {
            "mymod.py": _FIXED,
            "test_keep.py": "def test_keep():\n    assert False\n",
        },
        sandbox_provider=LocalSandboxProvider(),
        worktree_path=str(worktree),
    )
    assert res.resolved is False
    assert res.regressed == [_PTP]


@pytest.mark.asyncio
async def test_solve_fn_never_sees_hidden_tests(worktree: Path) -> None:
    seen: dict[str, object] = {}

    def solve(case: GoldenCase) -> dict[str, str]:
        seen["metadata"] = dict(case.metadata)
        return {"mymod.py": _FIXED}

    await run_swe_case(
        case=_case(fail=[_FTP], keep=[_PTP]),
        solve_fn=solve,
        sandbox_provider=LocalSandboxProvider(),
        worktree_path=str(worktree),
    )
    assert "fail_to_pass" not in seen["metadata"]  # type: ignore[operator]
    assert "pass_to_pass" not in seen["metadata"]  # type: ignore[operator]
