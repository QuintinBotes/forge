"""Sandboxed fail-to-pass / pass-to-pass runner for the Self-Eval Gate.

The runner is the ground-truth executor: it takes a minted case (with hidden
fail-to-pass + pass-to-pass tests), applies a candidate patch produced by a
``solve_fn``, and runs the hidden tests inside a real
:class:`~forge_contracts.sandbox.SandboxSession` (the local ``worktree``
provider offline; a stronger isolation kind in production). Nothing about the
hidden tests is ever handed to ``solve_fn`` — it only sees the case's public
prompt/metadata — so a model cannot game the gate by reading the checks.

``solve_fn(case) -> Mapping[path, content]`` returns the files the candidate
change writes into the worktree (a file-write patch; simple + deterministic to
verify). A case is *resolved* only when every ``fail_to_pass`` test now passes
AND no ``pass_to_pass`` test regresses.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from forge_contracts import SandboxKind, SandboxSpec
from forge_contracts.sandbox import SandboxProvider, SandboxSession
from forge_eval.benchmark.swe_case import SweCaseFields, parse_swe_case_fields
from forge_eval.golden import GoldenCase

#: A candidate solution as file writes (relative path -> new content).
SolveFn = Callable[[GoldenCase], Mapping[str, str]]

_DEFAULT_TIMEOUT_S = 600
_EVIDENCE_CAP = 4096
#: Case-metadata keys that must NEVER reach solve_fn (the model) — the hidden
#: tests the gate scores against.
_HIDDEN_KEYS = ("fail_to_pass", "pass_to_pass")


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


def _apply_patch(worktree: Path, files: Mapping[str, str]) -> None:
    """Write the candidate patch's files into the worktree (path-traversal safe)."""
    root = worktree.resolve()
    for rel, content in files.items():
        dest = (root / rel).resolve()
        if not dest.is_relative_to(root):
            raise ValueError(f"patch path escapes the worktree: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _test_command(test_id: str) -> str:
    """pytest node-id / path -> a deterministic, quiet invocation."""
    return f"python -m pytest -q -p no:cacheprovider {test_id}"


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
    _apply_patch(Path(worktree_path), patch)

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
    """Return the subset of ``test_ids`` that PASS (exit 0) in the sandbox."""
    passing: list[str] = []
    for test_id in test_ids:
        out = await session.run(
            _test_command(test_id), cwd=session.workspace_dir, timeout_s=timeout_s
        )
        if out.exit_code == 0:
            passing.append(test_id)
        else:
            evidence.append(f"{test_id}: exit {out.exit_code}\n{out.stdout}{out.stderr}")
    return passing
