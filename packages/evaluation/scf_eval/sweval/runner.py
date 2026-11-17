"""Sandboxed fail-to-pass / pass-to-pass runner for the Self-Eval Gate.

The runner is the ground-truth executor: it takes a minted case (with hidden
fail-to-pass + pass-to-pass tests), applies a candidate patch produced by a
``solve_fn``, and runs the hidden tests inside a real
:class:`~scf_contracts.sandbox.SandboxSession` (the local ``worktree``
provider offline; a stronger isolation kind in production). Nothing about the
hidden tests is ever handed to ``solve_fn`` — it only sees the case's public
prompt/metadata — so a model cannot game the gate by reading the checks.

``solve_fn(case) -> Mapping[path, content]`` returns the files the candidate
change writes into the worktree (a file-write patch; simple + deterministic to
verify). A case is *resolved* only when every ``fail_to_pass`` test now passes
AND no ``pass_to_pass`` test regresses.

The candidate's files are untrusted input to the scorer. A change that writes
pytest's configuration or shadows pytest is refused unscored, and a test counts
as passing only when pytest's own report says it ran and passed (a skip does not).
Candidate code still runs inside the test process, so a change built to tamper
with that process from within can still lie; the sandbox is the boundary there.
"""

from __future__ import annotations

import os
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from scf_contracts import SandboxKind, SandboxSpec
from scf_contracts.sandbox import SandboxProvider, SandboxSession
from scf_eval.benchmark.swe_case import SweCaseFields, parse_swe_case_fields
from scf_eval.golden import GoldenCase

#: A candidate solution as file writes (relative path -> new content).
SolveFn = Callable[[GoldenCase], Mapping[str, str]]

_DEFAULT_TIMEOUT_S = 600
_EVIDENCE_CAP = 4096
#: Case-metadata keys that must NEVER reach solve_fn (the model) — the hidden
#: tests the gate scores against, by id and by file.
_HIDDEN_KEYS = ("fail_to_pass", "pass_to_pass", "held_out")
#: File names, at any depth, that configure pytest or load into it before any test.
_PYTEST_CONFIG = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
#: Top-level names that shadow pytest itself (``python -m`` imports from the cwd first).
_PYTEST_SHADOWS = frozenset({"pytest.py", "pytest", "_pytest"})


@dataclass(frozen=True)
class SweCaseResult:
    """The outcome of running one minted case against a candidate patch."""

    case_id: str
    #: The fail-to-pass tests that PASS after the patch — used as ``output_ids``
    #: so the ``agent.fail_to_pass_rate`` metric (set overlap vs the case's
    #: ``expected_ids`` = all fail-to-pass) scores the resolution rate.
    output_ids: list[str]
    #: pass-to-pass tests that REGRESSED (were green, now fail) — any regression
    #: fails the case even if every fail-to-pass now passes.
    regressed: list[str] = field(default_factory=list)
    resolved: bool = False
    evidence: str = ""
    #: A setup command failed, so the case could not be scored at all.
    infra_error: bool = False
    #: Paths the change wrote that would alter how pytest runs; the change was refused.
    refused: list[str] = field(default_factory=list)


def _touches_pytest(rel: str) -> bool:
    """Whether writing ``rel`` would change how pytest runs or reports the tests."""
    norm = os.path.normpath(rel).lower()
    return PurePosixPath(norm).name in _PYTEST_CONFIG or norm.split("/")[0] in _PYTEST_SHADOWS


def _apply_patch(worktree: Path, files: Mapping[str, str]) -> None:
    """Write the candidate patch's files into the worktree (path-traversal safe)."""
    root = worktree.resolve()
    for rel, content in files.items():
        dest = (root / rel).resolve()
        if not dest.is_relative_to(root):
            raise ValueError(f"patch path escapes the worktree: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _test_command(test_id: str, report: str) -> str:
    """pytest node-id / path -> a deterministic, quiet invocation writing a JUnit report."""
    return f"python -m pytest -q -p no:cacheprovider --junitxml={report} {test_id}"


def _reported_passing(report: Path) -> bool:
    """True when pytest's report holds at least one test and every one of them passed."""
    try:
        cases = list(ET.parse(report).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return False
    bad = ("failure", "error", "skipped")
    return bool(cases) and all(case.find(tag) is None for case in cases for tag in bad)


async def run_swe_case(
    *,
    case: GoldenCase,
    solve_fn: SolveFn,
    sandbox_provider: SandboxProvider,
    worktree_path: str,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> SweCaseResult:
    """Apply ``solve_fn``'s patch and run the case's hidden tests in a sandbox.

    ``worktree_path`` is a prepared checkout at the case's ``base_commit``
    (the caller/miner sets it up); the runner writes the patch, runs
    ``setup_commands``, then the hidden fail-to-pass + pass-to-pass tests.
    """
    fields: SweCaseFields = parse_swe_case_fields(case)
    if not fields.fail_to_pass:
        raise ValueError(f"case {case.id!r} has no fail_to_pass tests to gate on")

    # solve_fn only ever sees a REDACTED case — the hidden test ids are stripped
    # so they can never enter a model's context (the harness reads them itself).
    public_case = replace(
        case,
        metadata={k: v for k, v in case.metadata.items() if k not in _HIDDEN_KEYS},
    )
    patch = solve_fn(public_case)
    refused = [rel for rel in patch if _touches_pytest(rel)]
    if refused:
        return SweCaseResult(
            case_id=case.id,
            output_ids=[],
            evidence=f"refused: the change writes pytest's own files: {', '.join(refused)}",
            refused=refused,
        )
    _apply_patch(Path(worktree_path), patch)
    # The held-out tests go in last, so the candidate can neither see nor replace them.
    _apply_patch(Path(worktree_path), {f.path: f.content for f in fields.held_out})

    spec = SandboxSpec(
        agent_run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=SandboxKind.WORKTREE,
        host_worktree_path=worktree_path,
        exec_timeout_seconds=timeout_s,
    )
    await sandbox_provider.preflight()
    session: SandboxSession = await sandbox_provider.create(spec)
    evidence_parts: list[str] = []
    try:
        for cmd in fields.setup_commands:
            out = await session.run(cmd, cwd=session.workspace_dir, timeout_s=timeout_s)
            if out.exit_code != 0:
                evidence_parts.append(f"setup failed: {cmd}\n{out.stdout}{out.stderr}")
                return SweCaseResult(
                    case_id=case.id,
                    output_ids=[],
                    resolved=False,
                    evidence="\n".join(evidence_parts)[-_EVIDENCE_CAP:],
                    infra_error=True,
                )

        passed = await _run_tests(session, fields.fail_to_pass, timeout_s, evidence_parts)
        regressed = [
            t
            for t in fields.pass_to_pass
            if t not in await _run_tests(session, [t], timeout_s, evidence_parts)
        ]
        resolved = set(passed) == set(fields.fail_to_pass) and not regressed
        return SweCaseResult(
            case_id=case.id,
            output_ids=passed,
            regressed=regressed,
            resolved=resolved,
            evidence="\n".join(evidence_parts)[-_EVIDENCE_CAP:],
        )
    finally:
        await session.teardown(reason="self_eval_complete")


async def _run_tests(
    session: SandboxSession,
    test_ids: Sequence[str],
    timeout_s: int,
    evidence: list[str],
) -> list[str]:
    """Return the subset of ``test_ids`` that pytest reports as run and passed."""
    passing: list[str] = []
    for test_id in test_ids:
        # A fresh name per run, so a change cannot plant a report there beforehand.
        report = f".scf-junit-{uuid.uuid4().hex}.xml"
        host_report = Path(session.host_worktree_path) / report
        out = await session.run(
            _test_command(test_id, report), cwd=session.workspace_dir, timeout_s=timeout_s
        )
        reported = _reported_passing(host_report)
        host_report.unlink(missing_ok=True)
        if out.exit_code == 0 and reported:
            passing.append(test_id)
        else:
            evidence.append(f"{test_id}: exit {out.exit_code}\n{out.stdout}{out.stderr}")
    return passing
